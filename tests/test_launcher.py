"""``contexto run claude`` (M2 lane A packet A3) with a fake ``claude`` executable. No live model."""

from __future__ import annotations

import fcntl
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
record = {"argv": args, "pid": os.getpid(), "env_base_url": os.environ.get("ANTHROPIC_BASE_URL"),
          "env_session": os.environ.get("CONTEXTO_SESSION")}
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
    headers = {"Content-Type": "application/json", "x-api-key": "fake-key"}
    if os.environ.get("FAKE_CLAUDE_SESSION"):  # Claude Code's own session id, the same as its transcript's name
        headers["X-Claude-Code-Session-Id"] = os.environ["FAKE_CLAUDE_SESSION"]
    request = urllib.request.Request(base + "/v1/messages", data=body, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        record["response_status"] = response.status
        response.read()  # to the end, as Claude Code does: the proxy logs the entry before the last bytes
    if os.environ.get("FAKE_SAY"):  # the CLI's own output while it still owns the terminal
        print(os.environ["FAKE_SAY"], file=sys.stderr, flush=True)
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
    assert server["command"] == sys.executable and server["args"] == ["-P", "-m", "contexto", "mcp", "--session=s1"]
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
    # D57: the default headroom floor is checked against the bound like an explicit one
    (["--bound", "10"], EXIT_USAGE, "--bound 10 is not above the headroom floor (25,000, the default)"),
    (["--bound", "25000"], EXIT_USAGE, "--bound 25,000 is not above the headroom floor (25,000, the default)"),
    (["--bound", "10", "--headroom-floor", "25000"], EXIT_USAGE,
     "--headroom-floor must be above 0 and below the bound (10), got 25,000"),
])
def test_usage_errors_exit_2_without_running_claude(env, upstream, tmp_path, args, code, message):
    proc = launch(env, "--upstream", upstream, *args, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == code and message in err
    assert "window budget" not in err  # refused before any warning about it
    assert not Path(env["FAKE_RECORD"]).exists()
    assert not (Path(env["CONTEXTO_HOME"]) / "sessions").exists()


def test_a_small_bound_above_the_floor_is_accepted_with_the_warning(env, upstream, tmp_path):
    proc = launch(env, "--upstream", upstream, "--bound", "25001", "--session", "small", "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0 and "window budget" in err


def test_a_session_id_that_only_starts_existing_ones_is_refused(env, upstream, tmp_path):
    """D57, reviewer B on PR #241: `--session 5238` made a new empty session that the readers then preferred
    (an exact id wins over a prefix) to the session 52385048... the user meant."""
    sessions = Path(env["CONTEXTO_HOME"]) / "sessions"
    for sid in ("52385048ed0d", "5238ffff"):
        open_session(Path(env["CONTEXTO_HOME"]), sid, tmp_path)
    proc = launch(env, "--upstream", upstream, "--session", "5238", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "Traceback" not in err
    assert "--session '5238' is not a session but the start of 2 sessions: 52385048ed0d, 5238ffff" in err
    assert sorted(p.name for p in sessions.iterdir()) == ["52385048ed0d", "5238ffff"]
    assert not Path(env["FAKE_RECORD"]).exists()
    # A full id is resumed, and a new id that no session starts with is created; claude is told which.
    for sid in ("52385048ed0d", "52385048ed0d-2"):
        proc = launch(env, "--upstream", upstream, "--session", sid, "claude", "-p", "hi", cwd=tmp_path)
        proc.communicate(timeout=30)
        assert proc.returncode == 0
        assert json.loads(Path(env["FAKE_RECORD"]).read_text())["env_session"] == sid
    assert sorted(p.name for p in sessions.iterdir()) == ["52385048ed0d", "52385048ed0d-2", "5238ffff"]


def test_claude_version_runs_without_a_session_or_proxy(env, tmp_path):
    """D57: `contexto run claude --version` opened a session and a proxy just to print the version."""
    proc = launch(env, "claude", "--version", cwd=tmp_path)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0 and "2.1.302 (Claude Code)" in out and err == ""
    assert not (Path(env["CONTEXTO_HOME"]) / "sessions").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
def test_an_unwritable_contexto_home_exits_1_not_2(env, upstream, tmp_path):
    """D57: 2 is a usage error; a store that cannot be opened is Contexto failing to start (1). A home that
    is a file is refused up front as a usage error (D55, test_a_contexto_home_that_is_a_file_is_refused_unchanged)."""
    home = Path(env["CONTEXTO_HOME"])
    home.mkdir()
    home.chmod(0o500)  # kept as the user made it (D55), so sessions/ cannot be created
    try:
        proc = launch(env, "--upstream", upstream, "claude", cwd=tmp_path)
        _, err = proc.communicate(timeout=30)
    finally:
        home.chmod(0o700)
    assert proc.returncode == 1 and "cannot open session" in err and "Traceback" not in err, err
    assert not Path(env["FAKE_RECORD"]).exists()


def test_an_invalid_upstream_leaves_no_session(env, tmp_path):
    """D57: the session was opened before the proxy refused the upstream, and stayed behind."""
    proc = launch(env, "--upstream", "ftp://x", "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "invalid upstream" in err
    sessions = Path(env["CONTEXTO_HOME"]) / "sessions"  # refused before any session opens (D55)
    assert not sessions.exists() or list(sessions.iterdir()) == []


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
    assert mcp_payload("s", tmp_path)["mcpServers"]["contexto"]["args"][-1] == "--session=s"
    # An id starting with "-" stays the option's value (security QA 2026-11-05, finding 7).
    assert mcp_payload("-x", tmp_path)["mcpServers"]["contexto"]["args"][-1] == "--session=-x"
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
    # D55: the assumed overhead is the measured one on 2.1.314 (18,641 to 27,925 with tool search on).
    assert ASSUMED_OVERHEAD_DEFAULT == 28_000
    assert start_warning(150_000, None, ASSUMED_OVERHEAD_DEFAULT) is None
    assert start_warning(150_000, None, ASSUMED_OVERHEAD_OPENRIG) is None
    assert start_warning(100_000, None, ASSUMED_OVERHEAD_DEFAULT) is None  # 47,000 of window budget
    warning = start_warning(70_000, None, ASSUMED_OVERHEAD_DEFAULT)
    assert "about 17,000" in warning and "below 25,000" in warning and "--bound 103,000" in warning
    assert "recall" in warning  # says plainly what a small window costs
    negative = start_warning(50_000, None, ASSUMED_OVERHEAD_DEFAULT)
    # The overhead is the top of the measured range, so a budget of 0 or less is a "may" (reviewer B, PR #240).
    assert "may leave no window budget" in negative and "recall may not fit at all" in negative
    assert assumed_overhead({}) == ASSUMED_OVERHEAD_DEFAULT
    assert assumed_overhead({"OPENRIG_SESSION_NAME": "x"}) == ASSUMED_OVERHEAD_OPENRIG
    quiet = launch(env, "--upstream", upstream, "--session", "w1", "claude", cwd=tmp_path)
    _, err = quiet.communicate(timeout=30)
    assert err.count("contexto:") == 1  # only the session line
    noisy = launch(env, "--upstream", upstream, "--bound", "70000", "--session", "w2", "claude", cwd=tmp_path)
    _, err = noisy.communicate(timeout=30)
    assert "window budget about 17,000" in err
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


# D55: resume through Contexto, signals, deferred notices, and launcher messages

CLAUDE_A = "0acd81f6-1f21-40f8-ab64-5c09545905cf"
CLAUDE_B = "f816b535-d6ba-4c7a-ace7-0fba5c7df367"


def session_from(err: str) -> str:
    [line] = [line for line in err.splitlines() if line.startswith("contexto: session ")]
    return line.split()[2]


def meta(env, session) -> dict:
    return json.loads((Path(env["CONTEXTO_HOME"]) / "sessions" / session / "meta.json").read_text())


def first_run(env, upstream, cwd, claude_session=CLAUDE_A, *args) -> str:
    """A launch without --session whose fake claude makes one request as Claude Code session ``claude_session``."""
    run_env = dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=claude_session)
    proc = launch(run_env, "--upstream", upstream, *args, "claude", "-p", "first", cwd=cwd)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    return session_from(err)


def test_a_launch_records_claude_s_session_id_and_prints_how_to_resume_through_contexto(env, upstream, tmp_path):
    proc = launch(dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_A), "--upstream", upstream, "--bound",
                  "120000", "claude", "-p", "first", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    session = session_from(err)
    assert list(meta(env, session)["cli_sessions"]["claude"]) == [CLAUDE_A]
    hint = err.splitlines()[-1]
    assert hint == (f"contexto: resume this conversation through Contexto with: contexto run --session {session} "
                    f"--bound 120000 --upstream {upstream} claude --resume {CLAUDE_A}")
    assert "claude_session" not in json.dumps(session_log(env, session))  # the id stays out of the log


def test_resume_by_claude_session_id_reuses_its_contexto_session(env, upstream, tmp_path):
    session = first_run(env, upstream, tmp_path)
    first_run(env, upstream, tmp_path, CLAUDE_B)  # another conversation, another Contexto session
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = launch(dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_A), "--upstream", upstream, "claude",
                  "--resume", CLAUDE_A, "-p", "second", cwd=elsewhere)  # by id: the directory does not matter
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert session_from(err) == session
    assert f"Claude Code session {CLAUDE_A} was served by Contexto session {session}" in err
    assert [e["event"] for e in session_log(env, session)].count("launch") == 2
    record = json.loads(Path(env["FAKE_RECORD"]).read_text())
    assert record["mcp"]["mcpServers"]["contexto"]["args"][-1] == f"--session={session}"


@pytest.mark.parametrize("form", [["--continue"], ["-c"]])
def test_continue_reuses_the_latest_contexto_session_of_this_directory(env, upstream, tmp_path, form):
    first_run(env, upstream, tmp_path, CLAUDE_A)
    latest = first_run(env, upstream, tmp_path, CLAUDE_B)
    proc = launch(dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_B), "--upstream", upstream, "claude",
                  *form, "-p", "again", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert session_from(err) == latest and "warning" not in err


def test_continue_elsewhere_and_an_unknown_resume_say_so_and_start_a_new_session(env, upstream, tmp_path):
    served = first_run(env, upstream, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    proc = launch(env, "--upstream", upstream, "claude", "--continue", cwd=other)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert session_from(err) != served
    assert "no Contexto session has served Claude Code in this directory" in err
    proc = launch(env, "--upstream", upstream, "claude", "--resume", CLAUDE_B, "-p", "x", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert session_from(err) != served and f"no Contexto session has served Claude Code session {CLAUDE_B}" in err
    proc = launch(env, "--upstream", upstream, "claude", "--resume", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert session_from(err) != served and "picker" in err


def test_an_explicit_session_wins_over_resume(env, upstream, tmp_path):
    first_run(env, upstream, tmp_path)
    proc = launch(env, "--upstream", upstream, "--session", "mine", "claude", "--resume", CLAUDE_A, cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert session_from(err) == "mine" and "served" not in err


def test_continue_that_lands_in_another_conversation_is_reported_at_exit(env, upstream, tmp_path):
    session = first_run(env, upstream, tmp_path, CLAUDE_A)
    # Claude Code continued a conversation this Contexto session never served (one run without Contexto).
    proc = launch(dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_B), "--upstream", upstream, "claude",
                  "--continue", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert session_from(err) == session
    assert f"Claude Code continued session {CLAUDE_B}" in err and "warning" in err


@pytest.mark.parametrize("args, expected", [
    (["-p", "x"], None), (["--continue"], ("continue", None)), (["-p", "-c", "x"], ("continue", None)),
    (["--resume", CLAUDE_A, "-p"], ("resume", CLAUDE_A)), (["-r", CLAUDE_A], ("resume", CLAUDE_A)),
    ([f"--resume={CLAUDE_A}"], ("resume", CLAUDE_A)), (["--resume"], ("resume", None)),
    (["--resume", "-p", "x"], ("resume", None)), (["-p", "--", "--continue"], None),
    # Reviewer A, PR #240: a value of an option that takes one is not an option, even with a leading "-".
    (["--append-system-prompt", "--continue"], None), (["--model", "-c", "-p"], None),
    (["--add-dir", "a", "-c"], ("continue", None)), (["--session-id", "-r", "x"], None),
    (["--prefill", "--continue"], None),  # a hidden option that takes a prompt
    # Short-option clusters, read as Claude Code's Commander parser does (reviewer A, PR #240 round 2).
    (["-pc", "hi"], ("continue", None)), (["-cp", "hi"], ("continue", None)),
    ([f"-r{CLAUDE_A}", "-p"], ("resume", CLAUDE_A)), ([f"-pr{CLAUDE_A}"], ("resume", CLAUDE_A)),
    (["-pr", CLAUDE_A], ("resume", CLAUDE_A)), (["-rc"], ("resume", "c")), (["-pdx", "-c"], ("continue", None)),
    (["-n", "-c"], None), (["-nmine", "-c"], ("continue", None)), (["-pz", "-c"], ("continue", None)),
    # D59 (review of #245): -h is Commander's help option, which a cluster does not look up; a lone "-" is a
    # session name; hidden options that take a value, read from the 2.1.314 and 2.1.318 binaries.
    (["-hc"], None), (["-ch"], ("continue", None)), (["-r", "-"], ("resume", "-")),
    (["--resume", "-"], ("resume", "-")),
    (["--thinking", "--continue"], None), (["--task-budget", "-c"], None), (["--workload", "-r", "x"], None),
    (["--session-began-as-copy", "-c"], None),
])
def test_resume_request(args, expected):
    assert launcher.resume_request(args) == expected


def test_claude_session_of_a_request():
    meta_user = json.dumps({"device_id": "d", "account_uuid": "", "session_id": CLAUDE_B})
    assert launcher.claude_session({"X-Claude-Code-Session-Id": CLAUDE_A}, {}) == CLAUDE_A
    assert launcher.claude_session({"x-claude-code-session-id": CLAUDE_A}, {}) == CLAUDE_A
    assert launcher.claude_session({}, {"metadata": {"user_id": meta_user}}) == CLAUDE_B
    assert launcher.claude_session({}, {"metadata": {"user_id": f"user_ab_account__session_{CLAUDE_B}"}}) == CLAUDE_B
    for headers, request in (({}, {}), ({"X-Claude-Code-Session-Id": "../x"}, {}),
                             ({}, {"metadata": {"user_id": "{not json"}}), ({}, {"metadata": "x"})):
        assert launcher.claude_session(headers, request) is None


def test_session_recorder_passes_attributes_and_never_fails_a_request(tmp_path):
    calls = []

    class Engine:
        preserves_input = True

        def __call__(self, request, headers, endpoint):
            calls.append(endpoint)
            return request, headers, []

        def tools_verdict(self):
            return None

    class BrokenStore:
        def record_cli_session(self, cli, cli_session, seen_at=None):
            raise OSError("disk full")

    recorder = launcher.SessionRecorder(Engine(), BrokenStore())
    assert recorder({"m": 1}, {"X-Claude-Code-Session-Id": CLAUDE_A}, "/v1/messages") == ({"m": 1}, {
        "X-Claude-Code-Session-Id": CLAUDE_A}, [])
    assert recorder.preserves_input is True and recorder.tools_verdict() is None
    assert recorder.last == CLAUDE_A and calls == ["/v1/messages"]


def test_contexto_options_after_claude_get_a_hint_and_nothing_starts(env, upstream, tmp_path):
    proc = launch(env, "--upstream", upstream, "claude", "--session", "foo", "--bound=1000", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE
    assert "--session after 'claude' is a Contexto option" in err
    assert "contexto run --session foo --bound=1000 claude -p hi" in err
    assert not Path(env["FAKE_RECORD"]).exists() and not (Path(env["CONTEXTO_HOME"]) / "sessions").exists()


def test_run_options_list_matches_the_parser():
    from contexto import cli

    parser = cli.build_parser()
    run = parser._subparsers._group_actions[0].choices["run"]
    names = {s for a in run._actions for s in a.option_strings if s.startswith("--") and s != "--help"}
    assert set(launcher.RUN_OPTIONS) == {n for n in names if not n.startswith("--codex")}


@pytest.mark.parametrize("how", ["flag", "env"])
def test_a_malformed_upstream_url_is_a_usage_error_naming_its_source(env, tmp_path, how):
    args = ["--upstream", "http://[::1"] if how == "flag" else []
    if how == "env":
        env["ANTHROPIC_BASE_URL"] = "http://[bad"
    proc = launch(env, *args, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "Traceback" not in err, err
    assert "invalid upstream" in err and f"(from {how})" in err
    assert not (Path(env["CONTEXTO_HOME"]) / "sessions").exists()  # no empty session left behind


def test_a_contexto_home_that_is_a_file_is_refused_unchanged(env, upstream, tmp_path):
    home = tmp_path / "home-file"
    home.write_text("x")
    home.chmod(0o644)
    env["CONTEXTO_HOME"] = str(home)
    proc = launch(env, "--upstream", upstream, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "is not a directory" in err, err
    assert stat.S_IMODE(home.stat().st_mode) == 0o644


def test_an_existing_contexto_home_keeps_its_mode(env, upstream, tmp_path):
    home = Path(env["CONTEXTO_HOME"])
    home.mkdir()
    home.chmod(0o750)
    proc = launch(env, "--upstream", upstream, "--session", "m1", "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert stat.S_IMODE(home.stat().st_mode) == 0o750
    assert stat.S_IMODE((home / "sessions").stat().st_mode) == 0o700  # Contexto's own directories stay private


@pytest.mark.parametrize("where", ["home", "session"])
def test_a_variable_reference_claude_code_would_expand_is_refused(env, upstream, tmp_path, where):
    # Claude Code 2.1.314 expands ${VAR} (not $VAR) in an MCP config's command, args and env, with no escape.
    args = ["--session", "s-${CTXP}"] if where == "session" else []
    if where == "home":
        env["CONTEXTO_HOME"] = str(tmp_path / "h-${CTXP}")
    proc = launch(dict(env, CTXP="X"), "--upstream", upstream, *args, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "${" in err and "would expand" in err, err
    assert not Path(env["FAKE_RECORD"]).exists()


def test_a_plain_dollar_is_kept(env, upstream, tmp_path):
    env["CONTEXTO_HOME"] = str(tmp_path / "h-$CTXP")
    proc = launch(env, "--upstream", upstream, "--session", "d1", "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err


@pytest.mark.parametrize("kind, message", [("not-executable", "is not executable"),
                                           ("broken-symlink", "is a broken symlink"),
                                           ("directory", "is a directory")])
def test_an_unusable_claude_is_named_with_what_is_wrong(env, upstream, tmp_path, kind, message):
    bad = tmp_path / "badbin"
    bad.mkdir()
    target = bad / "claude"
    if kind == "not-executable":
        target.write_text("#!/bin/sh\n")
        target.chmod(0o644)
    elif kind == "broken-symlink":
        target.symlink_to(tmp_path / "gone")
    else:
        target.mkdir()
    env["PATH"] = f"{bad}{os.pathsep}/usr/bin:/bin"
    proc = launch(env, "--upstream", upstream, "claude", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 127 and str(target) in err and message in err, err
    assert "not found on PATH" not in err


def test_notice_log_holds_messages_while_the_cli_owns_the_terminal(tmp_path):
    import io

    store, err = FakeStore(tmp_path, "s"), io.StringIO()
    notices = NoticeLog(store, err)
    notices.hold()
    small = {"event": "request", "transform_events": [{"event": "rotation", "fixed_overhead": 20_903,
                                                       "window_budget": 14_097}]}
    missing = {"event": "mcp_tools_missing", "dialect": "anthropic", "at": "request", "server_listed": False}
    notices.log(small)
    notices.log({"event": "request", "transform_events": [missing]})
    notices.log(dict(missing, at="exit", repeated=True))
    assert err.getvalue() == "" and len(store.events) == 3  # logged at once, shown later
    notices.release()
    lines = err.getvalue().splitlines()
    assert len(lines) == 2  # the exit-time repeat of a warning not yet shown adds nothing
    assert "measured window budget 14,097" in lines[0] and "without Contexto's tools" in lines[1]
    notices.log(small)
    notices.log({"event": "mcp_tools_denied", "tools": ["contexto_recall"]})
    assert len(err.getvalue().splitlines()) == 3  # released: shown at once


def _tmp_env(env, tmp_path) -> Path:
    tmp = tmp_path / "tmpdir"
    tmp.mkdir()
    env["TMPDIR"] = str(tmp)
    return tmp


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_signal_during_the_version_check_cleans_up(env, upstream, tmp_path, signum):
    tmp = _tmp_env(env, tmp_path)
    marker = tmp_path / "version-running"
    env["FAKE_VERSION_HANG"] = str(marker)
    env.pop("CONTEXTO_NO_VERSION_CHECK", None)
    proc = launch(env, "--upstream", upstream, "--session", "g1", "claude", cwd=tmp_path)
    wait_for(marker)
    proc.send_signal(signum)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 128 + signum and "Traceback" not in err, err
    assert not list(tmp.glob("contexto-run-*"))
    assert not Path(env["FAKE_RECORD"]).exists()
    events = session_log(env, "g1")
    assert events[-1] == dict(events[-1], event="exit", code=128 + signum)


@pytest.mark.parametrize("signum, child_gets", [(signal.SIGQUIT, signal.SIGQUIT), (signal.SIGUSR1, signal.SIGTERM),
                                                (signal.SIGUSR2, signal.SIGTERM)])
def test_other_terminating_signals_end_claude_and_clean_up(env, upstream, tmp_path, signum, child_gets):
    tmp = _tmp_env(env, tmp_path)
    env["FAKE_SLEEP"] = "60"
    proc = launch(env, "--upstream", upstream, "--session", "g2", "claude", cwd=tmp_path)
    wait_for(Path(env["FAKE_RECORD"]))
    proc.send_signal(signum)
    proc.communicate(timeout=30)
    assert proc.returncode == 128 + child_gets
    assert not list(tmp.glob("contexto-run-*"))
    assert session_log(env, "g2")[-1]["event"] == "exit"


def test_claude_does_not_outlive_a_killed_launcher(env, upstream, tmp_path):
    """SIGKILL cannot be caught: the watchdog sees the launcher's pipe close, ends claude with SIGTERM and
    removes the temporary files."""
    tmp = _tmp_env(env, tmp_path)
    env["FAKE_SLEEP"] = "120"
    proc = launch(env, "--upstream", upstream, "--session", "g3", "claude", cwd=tmp_path)
    wait_for(Path(env["FAKE_RECORD"]))
    pid = json.loads(Path(env["FAKE_RECORD"]).read_text())["pid"]
    try:
        proc.kill()
        proc.communicate(timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("claude outlived the launcher")
        while list(tmp.glob("contexto-run-*")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not list(tmp.glob("contexto-run-*"))
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_warning_raised_while_claude_runs_is_printed_after_it_exits(env, upstream, tmp_path):
    """D55: a line written while Claude Code's TUI owns the terminal lands in its input box."""
    env.update(FAKE_REQUEST="1", FAKE_TOOLS="Bash,mcp__contexto__contexto_search,mcp__contexto__contexto_recall",
               FAKE_DENIED="mcp__contexto__contexto_recall", FAKE_SAY="claude-still-running")
    proc = launch(env, "--upstream", upstream, "--session", "t4", "claude", "-p", "hello", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    warning = next(line for line in err.splitlines() if "refused Contexto's contexto_recall" in line)
    assert err.index("claude-still-running") < err.index(warning)


# Reviewer A on PR #240


@pytest.mark.parametrize("args", [["--resume", CLAUDE_A], ["--continue"]])
def test_an_automatically_picked_session_another_launch_has_open_is_not_shared(env, upstream, tmp_path, args):
    from contexto.store import SessionStore

    served = first_run(env, upstream, tmp_path)
    held = SessionStore(Path(env["CONTEXTO_HOME"]), served)  # holds live.lock, as a running launch does
    try:
        proc = launch(dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_A), "--upstream", upstream,
                      "claude", *args, "-p", "again", cwd=tmp_path)
        _, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        assert session_from(err) != served and "another launch has open" in err
        assert f"pass --session {served} before 'claude' to share it" in err
        # At exit: the conversation is now in two stores, and the earlier one is named.
        assert f"Claude Code session {CLAUDE_A} was also served by Contexto session {served}" in err
        proc = launch(env, "--upstream", upstream, "--session", served, "claude", "--resume", CLAUDE_A, cwd=tmp_path)
        _, err = proc.communicate(timeout=30)
        assert session_from(err) == served  # an explicit --session is never questioned
    finally:
        del held


def test_resume_names_the_other_sessions_that_served_the_id(tmp_path):
    import io

    from contexto.store import SessionStore

    for name, seen_at in (("older", 100.0), ("newer", 200.0)):
        store = SessionStore(tmp_path, name)
        store.record_cli_session("claude", CLAUDE_A, seen_at)
        del store
    err = io.StringIO()
    choice = launcher.choose_session(LaunchOptions(claude_args=["--resume", CLAUDE_A]), tmp_path, tmp_path, err)
    assert choice.session_id == "newer" and not choice.unmatched
    assert "served by Contexto session newer (newest; also served by older" in err.getvalue()


@pytest.mark.parametrize("fork", [False, True])
def test_continue_with_fork_session_is_not_reported_as_another_conversation(tmp_path, fork):
    import io

    class Recorder:
        last, seen = CLAUDE_B, {CLAUDE_B: 1.0}

    args = ["-c", "--fork-session"] if fork else ["-c"]
    err = io.StringIO()
    launcher.after_exit(LaunchOptions(claude_args=args), tmp_path, "s", Recorder(),
                        launcher.SessionChoice("s", continued={CLAUDE_A}), err)
    assert ("pages and pins come from another conversation" in err.getvalue()) is not fork
    assert err.getvalue().splitlines()[-1].endswith(f"claude --resume {CLAUDE_B}")


def test_flush_never_raises_even_when_the_seen_ids_change_under_it():
    class GrowingStore:
        def __init__(self, recorder):
            self.recorder = recorder

        def record_cli_session(self, cli, cli_session, seen_at=None):
            self.recorder.seen[f"late-{len(self.recorder.seen)}"] = 1.0  # a late handler thread adds an id

    recorder = launcher.SessionRecorder(lambda r, h, e: (r, h, []), None)
    recorder.store = GrowingStore(recorder)
    recorder.seen = {CLAUDE_A: 1.0, CLAUDE_B: 2.0}
    recorder.flush()


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


@pytest.mark.parametrize("forgotten", [False, True])
def test_the_watchdog_signals_only_a_child_it_was_not_told_has_exited(tmp_path, forgotten):
    from contexto.store.memory import MemoryStore

    tmp = tmp_path / "contexto-run-x"
    tmp.mkdir()
    child = _sleeper()
    try:
        watchdog = launcher.Watchdog.start(tmp, MemoryStore())
        watchdog.watch(child.pid)
        if forgotten:
            watchdog.forget()  # what run_child sends once the child is reaped: its pid may be reused
        watchdog.process.stdin.close()  # the launcher died without "done"
        watchdog.process.wait(timeout=10)
        assert not tmp.exists()
        if forgotten:
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.5)
        else:
            assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        child.kill()
        child.wait()


def test_a_signal_before_the_child_exists_is_forwarded_once_it_does():
    forwarding = launcher._ChildSignals()
    previous = forwarding.install()
    child = None
    try:
        forwarding._forward(signal.SIGUSR1, None)  # arrives during the spawn
        child = _sleeper()
        forwarding.attach(child)
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def test_run_child_tells_the_watchdog_the_child_was_reaped():
    sent = []

    class Watch:
        def watch(self, pid):
            sent.append(("pid", pid))

        def forget(self):
            sent.append(("exited",))

    assert launcher.run_child([sys.executable, "-c", "raise SystemExit(4)"], watchdog=Watch()) == 4
    assert [m[0] for m in sent] == ["pid", "exited"]


def test_an_empty_session_option_is_an_invalid_id(env, upstream, tmp_path):
    """D57: `--session ''` is refused, not taken as "not given" (with #240's resume choice in between)."""
    proc = launch(env, "--upstream", upstream, "--session", "", "claude", "-p", "hi", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_USAGE and "invalid session id: ''" in err and "Traceback" not in err, err
    assert not Path(env["FAKE_RECORD"]).exists()


# PR #240 round-2 follow-ups

RACE_PICK = """
import io, sys, time
from pathlib import Path
from contexto import launcher
home, cwd, go, done = map(Path, sys.argv[1:5])
while not go.exists():
    time.sleep(0.0005)
choice, store = launcher.pick_session(launcher.LaunchOptions(claude_args=["-c"]), home, cwd, io.StringIO())
print(choice.session_id, flush=True)
while not done.exists():  # keep the session open, as a running launch does
    time.sleep(0.01)
"""


def test_launches_started_together_never_share_an_automatically_picked_session(tmp_path):
    """Reviewer A: two `contexto run claude -c` started at once shared one store in 6 of 10 tries; the
    choice is now serialized on $CONTEXTO_HOME/choose.lock until the chosen session's live lock is held."""
    from contexto.store import SessionStore

    home, cwd = tmp_path / "home", tmp_path / "work"
    cwd.mkdir()
    seeded = SessionStore(home, "served")
    seeded.set_workdir(cwd)
    seeded.record_cli_session("claude", CLAUDE_A)
    del seeded
    for attempt in range(4):
        go, done = tmp_path / f"go{attempt}", tmp_path / f"done{attempt}"
        racers = [subprocess.Popen([sys.executable, "-c", RACE_PICK, str(home), str(cwd), str(go), str(done)],
                                   stdout=subprocess.PIPE, text=True) for _ in range(4)]
        try:
            time.sleep(0.5)  # every racer has started Python and is waiting
            go.touch()
            chosen = [racer.stdout.readline().strip() for racer in racers]
        finally:
            done.touch()
            for racer in racers:
                racer.communicate(timeout=30)
        assert chosen.count("served") == 1 and len(set(chosen)) == 4, chosen


def _hold_choose_lock(home: Path) -> int:
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / launcher.CHOOSE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_long_choose_lock_wait_and_its_timeout_are_said(tmp_path, monkeypatch, capsys):
    """Review of #245: a launch waited up to 5 s for choose.lock, then chose without it, saying nothing."""
    monkeypatch.setattr(launcher, "CHOOSE_LOCK_WAIT_S", 0.4)
    monkeypatch.setattr(launcher, "CHOOSE_LOCK_NOTICE_S", 0.1)
    home = tmp_path / "home"
    held = _hold_choose_lock(home)
    try:
        with launcher.choosing(home, sys.stderr):
            pass
    finally:
        os.close(held)
    err = capsys.readouterr().err
    assert "waiting for another contexto launch to choose its session" in err
    assert "choose.lock for 0.4 s; choosing without it, so two launches may pick the same session" in err
    with launcher.choosing(home, sys.stderr):  # a free lock: no wait, nothing said
        pass
    assert capsys.readouterr().err == ""


def test_ctrl_c_during_the_choose_lock_wait_exits_130_without_a_traceback(env, upstream, tmp_path):
    """Review of #245: Ctrl+C while another launch held choose.lock printed a raw traceback."""
    held = _hold_choose_lock(Path(env["CONTEXTO_HOME"]))
    try:
        proc = launch(env, "--upstream", upstream, "claude", "-c", cwd=tmp_path)
        lines = []
        while not any("waiting for another contexto launch" in line for line in lines):
            line = proc.stderr.readline()
            assert line, lines  # the launch must say it waits before it times out
            lines.append(line)
        proc.send_signal(signal.SIGINT)
        _, rest = proc.communicate(timeout=30)
    finally:
        os.close(held)
    err = "".join(lines) + rest
    assert proc.returncode == launcher.EXIT_INTERRUPTED, err
    assert "contexto: interrupted" in err and "Traceback" not in err
    assert not (Path(env["CONTEXTO_HOME"]) / "sessions").exists() or not any(
        (Path(env["CONTEXTO_HOME"]) / "sessions").iterdir())  # nothing was opened


def test_a_fork_says_it_shares_the_original_session(env, upstream, tmp_path):
    served = first_run(env, upstream, tmp_path)
    proc = launch(dict(env, FAKE_REQUEST="1", FAKE_CLAUDE_SESSION=CLAUDE_B), "--upstream", upstream, "claude",
                  "--resume", CLAUDE_A, "--fork-session", "-p", "fork", cwd=tmp_path)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert session_from(err) == served
    assert f"the fork shares Contexto session {served}" in err and "--session NEW" in err
    assert set(meta(env, served)["cli_sessions"]["claude"]) == {CLAUDE_A, CLAUDE_B}


def test_a_signal_during_a_failed_spawn_decides_the_exit_code(monkeypatch):
    def failing_popen(argv, env=None):
        os.kill(os.getpid(), signal.SIGTERM)  # arrives while the child is being started
        time.sleep(0.05)  # the handler runs
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(launcher.subprocess, "Popen", failing_popen)
    assert launcher.run_child(["claude-gone"]) == 128 + signal.SIGTERM


def test_the_watchdog_forgets_the_child_before_the_handlers_are_restored():
    seen = []

    class Watch:
        def watch(self, pid):
            pass

        def forget(self):
            seen.append(signal.getsignal(signal.SIGTERM))

    before = signal.getsignal(signal.SIGTERM)
    assert launcher.run_child([sys.executable, "-c", "pass"], watchdog=Watch()) == 0
    assert seen and seen[0] is not before and signal.getsignal(signal.SIGTERM) is before
