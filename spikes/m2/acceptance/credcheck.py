"""Zero-spend check that the acceptance run authenticates with the seat's apiKeyHelper, not OAuth.

Claude Code runs in default (non-bare) mode with the run's isolated CLAUDE_CONFIG_DIR, exactly as
the acceptance run will, and sends its first request to a local stub upstream that answers 401 and
never forwards. The stub compares the request's key header with the seat helper's output in
memory and keeps only booleans; no header value or key is printed, logged, or stored.

Until lane A's ``contexto run`` lands, this check passes the stub's URL with a ``--settings`` file,
which is what the launcher does; once it lands, ``command`` can be the launcher command.
"""

from __future__ import annotations

import hmac
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import driver  # noqa: E402


def _helper_output(seat_settings: Path) -> str:
    proc = subprocess.run([sys.executable, str(driver.KEYHELPER), str(seat_settings)], capture_output=True, text=True,
                          timeout=60)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("the seat's apiKeyHelper did not produce a key; output not shown")
    return proc.stdout.strip()


def check(seat_settings: Path, timeout: float = 90.0) -> dict[str, Any]:
    claude = shutil.which("claude")
    if claude is None:
        return {"ran": False, "reason": "claude CLI not installed"}
    if not seat_settings.is_file():
        return {"ran": False, "reason": "the seat settings file does not exist"}
    try:
        key = _helper_output(seat_settings)  # held in memory for the comparison only
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return {"ran": False, "reason": "the seat's apiKeyHelper did not produce a key; output not shown"}
    seen: dict[str, Any] = {}

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/v1/messages") and not seen:
                x_api_key = self.headers.get("x-api-key") or ""
                auth = self.headers.get("authorization") or ""
                beta = (self.headers.get("anthropic-beta") or "").lower()
                seen.update({
                    "path": self.path.split("?")[0],
                    "x_api_key_present": bool(x_api_key),
                    "authorization_present": bool(auth),
                    "oauth_beta_header": "oauth" in beta,
                    "key_matches_seat_helper": hmac.compare_digest(x_api_key, key)  # noqa: F821 - closure; del runs later
                    or hmac.compare_digest(auth, f"Bearer {key}"),  # noqa: F821 - as above
                })
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = b'{"type":"error","error":{"type":"authentication_error","message":"stub"}}'
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="contexto-credcheck-"))
    try:
        if driver.inside_git(tmp):
            return {"ran": False, "reason": "temporary directory is inside a git work tree"}
        config = driver.isolated_config(tmp, seat_settings)
        settings = tmp / "launcher-settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{httpd.server_port}"}}))
        env = driver.child_env(tmp / "home", config)
        env.pop("ANTHROPIC_BASE_URL", None)
        proc = subprocess.Popen([claude, "-p", "--settings", str(settings), "--model", driver.MODEL,
                                 "--output-format", "json", "say hi"],
                                cwd=tmp, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and proc.poll() is None and not seen:
            time.sleep(0.2)
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    del key
    result: dict[str, Any] = {"ran": True, "request_seen": bool(seen), **seen}
    result["pass"] = bool(seen) and seen.get("key_matches_seat_helper", False) and not seen.get("oauth_beta_header", True)
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: credcheck.py <seat settings file>", file=sys.stderr)
        return 2
    result = check(Path(argv[0]))
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
