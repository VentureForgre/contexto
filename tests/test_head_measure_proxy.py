"""D49 amended, through the real proxy: rule 4 measures the head right whenever the first usage arrives.

A stub upstream counts input tokens from the bytes it receives, at a ratio the estimator does not know
(3.2 bytes per token), and answers each request as scripted: with usage, with a 200 and no usage, with
usage that has ``output_tokens`` alone, or with a 529 that the client retries with the same body. The
engine is the real ``RewriteEngine``; nothing is sent to a model.

PR #244 review B, on main through the proxy: first usage at request 8 measured a 22,245-token head as 0
at bound 60,000; a first request resumed with no epoch record measured 25,365 as 0 at 150,000; usage
without an input count sent turn starts at 30,586 to 33,692 real tokens at bound 30,000."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from contexto.canonical import canonical_bytes
from contexto.rewrite import Config, RewriteEngine
from contexto.store.memory import MemoryStore
from test_head_change import LIVE_SYSTEM, SEAT_SYSTEM, base, big_server, main_state, no_change
from test_proxy_server import AUTH, call
from test_rewrite import FILL, assistant, history, tool_result, tool_use, user

STUB_RATIO = 3.2  # the stub's bytes per token; the estimator's seeds are 3.5 for text and 1.5 for tool output
QA = Config(bound=30_000, headroom_floor=5_000, min_turn_room=1_000, pin_budget=200, index_budget=200)
SEAT = Config(bound=30_000, headroom_floor=2_000, min_turn_room=6_000, pin_budget=200, index_budget=200)


class CountingUpstream(BaseHTTPRequestHandler):
    """Answers ``/v1/messages`` with usage counted from the request's bytes, as ``script`` says."""

    protocol_version = "HTTP/1.1"
    script: list[str] = []  # one reply per request: "usage", "no usage", "output only", "529"
    received: list[int] = []  # each request's counted input tokens

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        tokens = int(len(body) / STUB_RATIO)
        reply = CountingUpstream.script.pop(0) if CountingUpstream.script else "usage"
        if reply == "529":
            self._json(529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
            return
        CountingUpstream.received.append(tokens)
        message = {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "end_turn",
                   "content": [{"type": "text", "text": "synthetic answer"}]}
        if reply == "usage":
            message["usage"] = {"input_tokens": tokens, "output_tokens": 5}
        elif reply == "output only":
            message["usage"] = {"output_tokens": 5}
        self._json(200, message)

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def counting_upstream():
    CountingUpstream.script, CountingUpstream.received = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class Session:
    """A Claude Code shaped conversation sent through the proxy, with the stub's replies scripted."""

    def __init__(self, make_proxy, url, config, system_text, change=no_change):
        self.engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=config)
        self.proxy, self.log = make_proxy(upstream=url, transform=self.engine,
                                          observe_usage=self.engine.observe_usage)
        self.config, self.system_text, self.change = config, system_text, change

    def body(self, msgs, changed):
        req = base(msgs, self.system_text)
        return {**(self.change(req) if changed else req), "stream": False}

    def send(self, msgs, *replies, changed=False):
        """One client request (sent again after each 529, as Claude Code retries); its counted tokens."""
        CountingUpstream.script.extend(replies)
        data = json.dumps(self.body(msgs, changed)).encode()
        response, _ = call(self.proxy, "POST", "/v1/messages", data, AUTH)
        while response.status == 529:
            response, _ = call(self.proxy, "POST", "/v1/messages", data, AUTH)
        assert response.status == 200  # the proxy completes a response only after its usage is fed back
        return CountingUpstream.received[-1]

    def real_head(self, msgs, changed=True):
        req = self.body(msgs, changed)
        return len(canonical_bytes({"system": req["system"], "tools": req["tools"]})) / STUB_RATIO

    def turn_room_low(self):
        events = [e for entry in self.log.requests(0) for e in entry.get("transform_events") or []]
        return any(e.get("event") == "turn_room_low" for e in events)

    def assert_turn_start(self, msgs, real):
        """The measured head is at least the real head, and the turn start is under the bound by the stub's
        count, with the minimum room or ``turn_room_low`` logged."""
        head = main_state(self.engine).head_tokens
        assert head is None or head >= self.real_head(msgs), (head, self.real_head(msgs))
        assert real <= self.config.bound, real
        room = self.config.bound - real - self.config.headroom_floor
        assert room >= self.config.min_turn_room or self.turn_room_low(), room


def loop(session, msgs, start, steps, reply="usage", connect_at=None):
    for i in range(start, start + steps):
        msgs = msgs + [tool_use(i), tool_result(i, FILL)]
        session.send(msgs, reply, changed=connect_at is not None and i >= connect_at)
    return msgs


@pytest.mark.parametrize("missing", ["no usage", "output only", "529"])
@pytest.mark.parametrize("system_text, config, change", [(SEAT_SYSTEM, SEAT, big_server),
                                                         (LIVE_SYSTEM, Config(bound=60_000), big_server),
                                                         (LIVE_SYSTEM, QA, no_change)])
@pytest.mark.parametrize("late", [1, 8, 12])
def test_a_late_first_usage_through_the_proxy_measures_the_whole_head(make_proxy, counting_upstream, missing,
                                                                     system_text, config, change, late):
    session = Session(make_proxy, counting_upstream, config, system_text, change)
    msgs = history(2) + [user("long task")]
    session.send(msgs, missing)
    msgs = loop(session, msgs, 0, late - 1, missing, connect_at=6)
    msgs = loop(session, msgs, late - 1, 13 - late, connect_at=6)  # usage from request ``late`` on
    if missing == "529":  # a 529 is retried with the same body, and the retry is measured
        assert main_state(session.engine).head_tokens is not None
    n = 12
    for t in range(4):
        msgs = msgs + [assistant("done"), user(f"next {t}")]
        session.assert_turn_start(msgs, session.send(msgs, changed=True))
        msgs = loop(session, msgs, n, 6, connect_at=0)
        n += 6
    assert main_state(session.engine).head_tokens >= session.real_head(msgs)


@pytest.mark.parametrize("bound, reads", [(60_000, 10), (150_000, 18), (150_000, 40)])
def test_a_first_request_with_a_long_history_through_the_proxy_measures_the_whole_head(make_proxy,
                                                                                      counting_upstream, bound,
                                                                                      reads):
    # A conversation the engine has no epoch record for (a restarted proxy, --resume after a CLI upgrade
    # or a settings change) starts at k == 0 on its whole history; in the middle of a loop it is measured.
    system_text = LIVE_SYSTEM + "synthetic resumed session line\n" * 600  # about 25,000 tokens
    session = Session(make_proxy, counting_upstream, Config(bound=bound), system_text)
    msgs = history(3) + [user("long task")]
    for i in range(reads):
        msgs = msgs + [tool_use(i), tool_result(i, FILL)]
    session.send(msgs)
    assert main_state(session.engine).k == 0
    assert main_state(session.engine).head_tokens >= session.real_head(msgs, changed=False)
    n = reads
    for t in range(3):
        msgs = msgs + [assistant("done"), user(f"next {t}")]
        session.assert_turn_start(msgs, session.send(msgs))
        msgs = loop(session, msgs, n, 8)
        n += 8
