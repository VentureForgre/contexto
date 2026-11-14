"""What ``contexto serve`` adds to a request, against a stub upstream. Zero spend: nothing leaves 127.0.0.1.

    uv run python spikes/m2/serve/bench_serve.py [--requests 400] [--sessions 20] [--per-session 50]

Two measurements, each on a temporary ``CONTEXTO_HOME``, with the rewrite engine and a real session
store on both sides:

1. Latency. The same Messages request (about 60 KB, one Claude Code session) sent ``--requests``
   times through a plain ``ContextoProxy`` started as ``contexto run claude`` starts it, and the same
   number of times through ``contexto serve``, alternating, one new connection per request. Prints
   the median, the 90th centile, and the difference of the medians.
2. Throughput. ``--sessions`` Claude Code session ids, one thread each, each sending
   ``--per-session`` requests through ``serve`` at once. Prints requests per second, and the same
   number for one session sending alone.

The stub answers at once with a small JSON message, so the numbers are the proxy's own cost.
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import statistics
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from contexto import serve
from contexto.launcher import DEFAULT_BOUND, open_session, start_proxy
from contexto.upstream import Upstream

ANSWER = json.dumps({"id": "msg_bench", "type": "message", "role": "assistant", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "ok"}],
                     "usage": {"input_tokens": 15000, "output_tokens": 5, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}).encode()


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(ANSWER)))
        self.end_headers()
        self.wfile.write(ANSWER)


class Backlog(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True


def body(session: str, size: int = 60_000) -> bytes:
    """A Claude Code shaped request of about ``size`` bytes: 20 turns of tool output."""
    turns = []
    per_turn = size // 20
    for n in range(20):
        turns += [{"role": "user", "content": [{"type": "text", "text": f"prompt {n}"}]},
                  {"role": "assistant", "content": [{"type": "text", "text": f"answer {n} " + "x " * (per_turn // 2)}]}]
    turns.append({"role": "user", "content": [{"type": "text", "text": "the newest prompt"}]})
    request = {"model": "claude-haiku-5-5", "max_tokens": 1000,
               "system": [{"type": "text", "text": "You are Claude Code."}],
               "tools": [{"name": "Read", "description": "Reads a file",
                          "input_schema": {"type": "object", "properties": {}}}],
               "messages": turns, "stream": False,
               "metadata": {"user_id": json.dumps({"device_id": "d" * 64, "account_uuid": "", "session_id": session})}}
    return json.dumps(request, separators=(",", ":")).encode()


def send(port: int, path: str, data: bytes, session: str) -> float:
    started = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", path, body=data, headers={"Content-Type": "application/json", "x-api-key": "stub-key",
                                                    "X-Claude-Code-Session-Id": session,
                                                    "User-Agent": "claude-cli/2.1.322 (external, cli)"})
    response = conn.getresponse()
    response.read()
    conn.close()
    if response.status != 200:
        raise SystemExit(f"status {response.status} from port {port}")
    return (time.perf_counter() - started) * 1000


def centile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--per-session", type=int, default=50)
    args = parser.parse_args()
    stub = Backlog(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    upstream = f"http://127.0.0.1:{stub.server_address[1]}"
    with tempfile.TemporaryDirectory(prefix="contexto-bench-") as tmp:
        home = Path(tmp) / "home"
        config = {"bound": DEFAULT_BOUND}
        plain_session = str(uuid.uuid4())
        store = open_session(home, uuid.uuid4().hex, Path(tmp))
        plain, _ = start_proxy(store, upstream, "flag", config, False, stderr=io.StringIO(), cli="claude")
        service = serve.ServeService(home, Upstream(upstream, "flag"), serve.ensure_token(home), config=config,
                                     stderr=io.StringIO())
        service.start(0)
        try:
            serve_session = str(uuid.uuid4())
            through_plain, through_serve = body(plain_session), body(serve_session)
            for _ in range(20):  # warm both paths, and open the serve session
                send(plain.port, "/v1/messages?beta=true", through_plain, plain_session)
                send(service.port, f"/{service.token}/v1/messages?beta=true", through_serve, serve_session)
            plain_ms, serve_ms = [], []
            for _ in range(args.requests):
                plain_ms.append(send(plain.port, "/v1/messages?beta=true", through_plain, plain_session))
                serve_ms.append(send(service.port, f"/{service.token}/v1/messages?beta=true", through_serve,
                                     serve_session))
            print(f"latency, {args.requests} requests each, {len(through_plain):,} byte body:")
            for name, values in (("plain ContextoProxy", plain_ms), ("contexto serve", serve_ms)):
                print(f"  {name:20} median {statistics.median(values):.2f} ms, p90 {centile(values, 0.9):.2f} ms")
            print(f"  added by serve       {statistics.median(serve_ms) - statistics.median(plain_ms):+.2f} ms "
                  f"(difference of the medians)")

            started = time.perf_counter()
            for _ in range(args.per_session):
                send(service.port, f"/{service.token}/v1/messages?beta=true", through_serve, serve_session)
            alone = args.per_session / (time.perf_counter() - started)

            ids = [str(uuid.uuid4()) for _ in range(args.sessions)]
            bodies = {session: body(session) for session in ids}
            for session in ids:  # open every session first: the measurement is of requests, not opens
                send(service.port, f"/{service.token}/v1/messages?beta=true", bodies[session], session)
            times: list[float] = []
            lock = threading.Lock()

            def worker(session: str) -> None:
                mine = [send(service.port, f"/{service.token}/v1/messages?beta=true", bodies[session], session)
                        for _ in range(args.per_session)]
                with lock:
                    times.extend(mine)

            threads = [threading.Thread(target=worker, args=(session,)) for session in ids]
            started = time.perf_counter()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            elapsed = time.perf_counter() - started
            total = args.sessions * args.per_session
            print(f"throughput through serve, {total} requests:")
            print(f"  one session alone        {alone:.0f} requests/s")
            print(f"  {args.sessions} sessions at once      {total / elapsed:.0f} requests/s "
                  f"(median {statistics.median(times):.2f} ms, p90 {centile(times, 0.9):.2f} ms per request)")
            print(f"  open sessions            {service.health()['sessions']}")
        finally:
            service.stop()
            plain.stop()
            stub.shutdown()
            stub.server_close()


if __name__ == "__main__":
    main()
