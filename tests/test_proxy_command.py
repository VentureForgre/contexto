"""``contexto proxy`` subcommand: binds loopback, reports the port, logs to the session store."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from contexto import cli


class Hello(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_help_lists_the_proxy_flags(capsys):
    try:
        cli.main(["proxy", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--observe-thinking" in out and "--upstream" in out and "--port" in out


def first_line(stream, timeout: float) -> str:
    """The stream's first line, or "" if none arrives within the deadline (readline alone has none)."""
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    return lines[0].strip() if lines else ""


def test_proxy_command_serves_and_logs_to_the_session(tmp_path: Path, sigint_default):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Hello)
    threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    env = dict(os.environ, CONTEXTO_HOME=str(tmp_path / "home"))
    url = f"http://127.0.0.1:{upstream.server_address[1]}"
    proc = subprocess.Popen([sys.executable, "-m", "contexto", "proxy", "--session", "s1", "--upstream", url],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=tmp_path)
    try:
        line = first_line(proc.stdout, timeout=60)
        words = line.split()
        assert words[:2] == ["listening", "on"] and words[3:] == ["session", "s1", "upstream", "flag"], line
        host, port = words[2].rsplit(":", 1)
        assert host == "127.0.0.1"
        conn = http.client.HTTPConnection(host, int(port), timeout=60)
        conn.request("HEAD", "/api/hello")
        assert conn.getresponse().status == 200
        conn.close()
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=60)  # Ctrl+C is how the proxy is stopped; it must exit on it
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        upstream.shutdown()
        upstream.server_close()
    log = (tmp_path / "home" / "sessions" / "s1" / "log.jsonl").read_text()
    events = [json.loads(e) for e in log.splitlines()]
    assert events[0]["event"] == "proxy_start" and events[0]["upstream_source"] == "flag"
    assert any(e["event"] == "request" and e["path"] == "/api/hello" and e["status"] == 200 for e in events)
    assert url not in log and str(tmp_path) not in log
