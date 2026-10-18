"""Per-request CPU work in the proxy and the rewrite engine (spikes/m2/scale/proxy, "Before and after").

Each message's rule 10 hash is computed once per request, the request's turn starts are scanned once,
and a transform that declares ``preserves_input`` (the rewrite engine) is given the parsed request
without the proxy's defensive copy or its reference serialization. None of it may change a byte:
the equivalence test drives one synthetic session with rotations, tool loops, a paged-history
resync, count_tokens, and a proxy restart that resumes the epoch, through the real proxy and a local
upstream, once on the old paths and once on the new, and compares every upstream body, every log
event, the engine state, and the store. All content is synthetic; nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import http.client
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from contexto import proxy as proxy_mod
from contexto import rewrite as rw
from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES
from contexto.proxy import ContextoProxy, same_wire, serialize_request
from contexto.rewrite import ANTHROPIC, Config, RewriteEngine, message_hash, request_scope
from contexto.store.memory import MemoryStore

from test_rewrite import FILL, assistant, request, system, tool_result, tool_use, user

CONFIG = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
VOLATILE_KEYS = frozenset({"ts", "duration_ms", "elapsed_ms", "phase_ms", "port"})  # clock and socket


def reference_hash(message: dict, dialect: rw.Dialect) -> str:
    """Rule 10's hash as written before the fast path: normalise, canonical JSON, SHA-256."""
    return hashlib.sha256(canonical_bytes(dialect.normalise(message))).hexdigest()


# Hashing ------------------------------------------------------------------------------------------

HASH_CORPUS = [
    user("plain prompt"),
    user("prompt with context", context=True),
    {"role": "user", "content": "a plain string prompt"},
    {"role": "user", "content": [{"type": "text", "text": "one block", "cache_control": {"type": "ephemeral"}}]},
    {"role": "user", "content": [{"type": "text", "text": "one block, no cache"}]},
    {"role": "user", "content": [{"text": "keys in another order", "type": "text"}]},
    {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b",
                                                                  "cache_control": {"type": "ephemeral"}}]},
    system(),
    system(cache=False),
    tool_use(1, think="s1"),
    tool_result(2, "output with \"cache_control\" inside the text"),
    tool_result(3, "cache_control"),
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "cache_control": {"type": "ephemeral"},
                                  "content": [{"type": "text", "text": "nested", "cache_control": {"x": 1}}]}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Write",
                                       "input": {"cache_control": "a tool input key", "deep": [{"cache_control": 1}]}}]},
    {"role": "user", "content": [{"type": "text", "text": "non-ASCII: café, 日本語, emoji 😀"}]},
    {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "日本語 \\ud800 escaped"}]},
    {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "AAAA"}}]},
    {"role": "user", "content": [{"type": "text", "text": "single", "extra": True}]},
    {"role": "user", "content": [{"type": "text", "text": 5}]},
    {"role": "system", "content": [], "output_config": {"effort": "high"}},
    {"role": "user", "content": []},
    {"role": "user"},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Responses item"}]},
]


@pytest.mark.parametrize("message", HASH_CORPUS)
def test_hash_is_rule_10s_normalised_hash_on_every_path(message):
    expected = reference_hash(message, ANTHROPIC)
    assert message_hash(message) == expected
    with request_scope([message]):
        assert message_hash(message) == expected
        assert message_hash(message) == expected  # the memoised value
    token = rw._DIALECT.set(RESPONSES)
    try:
        assert message_hash(message) == reference_hash(message, RESPONSES)
        with request_scope([message]):
            assert message_hash(message) == reference_hash(message, RESPONSES)
    finally:
        rw._DIALECT.reset(token)


def test_a_lone_surrogate_fails_to_hash_on_both_paths_alike():
    message = {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "lone \ud800"}]}
    with pytest.raises(UnicodeEncodeError):
        reference_hash(message, ANTHROPIC)
    with pytest.raises(UnicodeEncodeError):
        message_hash(message)


def test_a_lone_surrogate_only_inside_cache_control_hashes_like_the_reference():
    # Normalisation drops cache_control, so the reference path never encodes the surrogate; the fast
    # path must fall back to it rather than raise (reviewer-1 on #205).
    message = json.loads(r'{"role":"user","content":[{"type":"text","text":"hi",'
                         r'"cache_control":{"type":"ephemeral","ttl":"\ud800"}}]}')
    assert message_hash(message) == reference_hash(message, ANTHROPIC)
    with request_scope([message]):
        assert message_hash(message) == reference_hash(message, ANTHROPIC)


def test_cache_control_and_string_conversion_still_hash_equal():
    plain = {"role": "user", "content": "the prompt"}
    block = {"role": "user", "content": [{"type": "text", "text": "the prompt", "cache_control": {"type": "ephemeral"}}]}
    with request_scope([plain, block]):
        assert message_hash(plain) == message_hash(block)
    moved = copy.deepcopy(tool_result(1))
    moved["content"][0]["cache_control"] = {"type": "ephemeral"}
    assert message_hash(moved) == message_hash(tool_result(1))


def test_the_memo_is_per_object_and_per_dialect():
    a, b = user("same text"), user("same text")
    with request_scope([a, b]):
        assert message_hash(a) == message_hash(b)
        token = rw._DIALECT.set(RESPONSES)
        try:
            assert message_hash(a) == reference_hash(a, RESPONSES)
        finally:
            rw._DIALECT.reset(token)
        assert message_hash(a) == reference_hash(a, ANTHROPIC)
    assert rw._MEMO.get() is None


def counting(monkeypatch, name: str) -> list[int]:
    calls = [0]
    real = getattr(rw, name)

    def wrapper(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(rw, name, wrapper)
    return calls


def tool_loop_session(turns: int) -> list[dict]:
    msgs = [user(f"prompt 0 {FILL}", context=True), system()]
    for t in range(turns):
        if t:
            msgs.append(user(f"prompt {t} {FILL}"))
        msgs += [tool_use(f"{t}", think=f"t{t}"), tool_result(f"{t}", f"out {t} {FILL}"),
                 assistant(f"answer {t} {FILL}")]
    return msgs


def step(engine: RewriteEngine, messages: list[dict]) -> dict:
    """One request as the proxy makes it: the transform, then the usage for the bytes it sent."""
    req = request(messages)
    upstream, _, _ = engine(req, {}, rw.MESSAGES)
    tokens = math.ceil(len(serialize_request(upstream)) / 2)
    engine.observe_usage(rw.MESSAGES, {"input_tokens": 2, "cache_read_input_tokens": tokens - 2}, req)
    return req


def test_a_tool_loop_request_hashes_each_message_once_and_scans_turns_once(monkeypatch):
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    msgs = tool_loop_session(12)
    for n in range(3, len(msgs) + 1):
        step(engine, msgs[:n])
    msgs += [user(f"prompt new {FILL}"), tool_use("x"), tool_result("x")]
    step(engine, msgs[:-2])
    k = engine.states[next(iter(engine.states))].k
    assert k > 0  # rotated: carried_content scans the paged region
    hashes = counting(monkeypatch, "_message_hash")
    starts = counting(monkeypatch, "_anthropic_is_turn_start")
    req = request(msgs)
    engine(req, {}, rw.MESSAGES)
    assert hashes[0] == len(msgs)  # on the old path 2 * len(msgs)
    # One scan of the request, carried_content's scan of the paged region M[:k], and per-message checks
    # of the retained window M[k:] (the snapshot's turn count, the window estimate); on the old path
    # nine scans of the whole request (468 checks here).
    assert starts[0] <= len(msgs) + k + 2 * (len(msgs) - k)
    hashes[0] = starts[0] = 0
    engine.observe_usage(rw.MESSAGES, {"input_tokens": 100}, req)
    assert hashes[0] == 2  # the conversation key (first message) and usage attribution (last message)
    assert starts[0] == len(msgs)  # one scan for the main-conversation check and the turn id (was two)


# The unchanged-request check ----------------------------------------------------------------------

SURROGATE = "lone \ud800 surrogate"
WIRE_PAIRS = [
    (1, 1), (1, 1.0), (True, 1), (False, 0), (None, None), (0.0, -0.0), (1.5, 1.5), (float("nan"), float("nan")),
    (float("inf"), float("inf")), (10**16, 1e16), ("a", "a"), ("a", "b"), (SURROGATE, SURROGATE),
    (SURROGATE, "lone \\ud800 surrogate"), ("\U0001F600", "\ud83d\ude00"),
    ({"a": 1, "b": 2}, {"a": 1, "b": 2}), ({"a": 1, "b": 2}, {"b": 2, "a": 1}), ({"a": 1}, {"a": 1, "b": 2}),
    ({"a": [1, 2]}, {"a": [1, 2]}), ({"a": [1, 2]}, {"a": [1, 2, 3]}), ([1, [2, [3]]], [1, [2, [3]]]),
    ([1, 2], (1, 2)), ({1: "x"}, {"1": "x"}), ({"a": (1,)}, {"a": [1]}), ({"a": {1, 2}}, {"a": [1, 2]}),
    ({"a": {1, 2}}, {"b": 1}), ([], {}), ("", None),
]


@pytest.mark.parametrize(("a", "b"), WIRE_PAIRS)
def test_same_wire_is_exactly_serialized_equality(a, b):
    def wire(x):
        try:
            return serialize_request(x)
        except (TypeError, ValueError):
            return None

    expected = wire(a) is not None and wire(a) == wire(b)
    assert same_wire(a, b) is expected
    assert same_wire(b, a) is expected


def test_same_wire_falls_back_on_nesting_too_deep_to_walk():
    deep_a: Any = "leaf"
    deep_b: Any = "leaf"
    for _ in range(5_000):
        deep_a, deep_b = [deep_a], [deep_b]
    try:
        expected = serialize_request(deep_a) == serialize_request(deep_b)
    except RecursionError:
        expected = False
    assert same_wire(deep_a, deep_b) is expected


def test_same_wire_on_engine_output_checks_identity_not_bytes():
    msgs = tool_loop_session(3) + [user("new turn")]
    req = json.loads(json.dumps(request(msgs)))
    up, state, _ = rw.rewrite(req, None, MemoryStore(key=b"k" * 32), Config(), rw.TokenEstimator())
    assert state.k == 0
    assert up is not req and same_wire(up, req)  # k = 0: the same messages in a new list
    changed = {**req, "messages": req["messages"][1:]}
    assert not same_wire(changed, req)


# The proxy's transform step -----------------------------------------------------------------------

class Recorder(BaseHTTPRequestHandler):
    """A local Messages API stand-in: usage is ceil(request bytes / 2), so sizing follows what is sent."""

    protocol_version = "HTTP/1.1"
    bodies: list[tuple[str, bytes]] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Recorder.bodies.append((self.path, body))
        if self.path.startswith("/v1/messages/count_tokens"):
            payload = json.dumps({"input_tokens": math.ceil(len(body) / 2)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        tokens = math.ceil(len(body) / 2)
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_x", "type": "message", "role": "assistant", "content": [],
                "usage": {"input_tokens": 2, "cache_read_input_tokens": tokens - 2, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "synthetic reply"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = b"".join(f"event: {name}\ndata: {json.dumps(p)}\n\n".encode() for name, p in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def recorder():
    Recorder.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def post(proxy: ContextoProxy, path: str, body: bytes) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json", "x-api-key": "synthetic"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    assert response.status == 200
    return data


class Spy:
    def __init__(self, preserves: bool, mode: str) -> None:
        self.preserves_input = preserves
        self.mode = mode
        self.seen: list[dict] = []
        self.observed: list[dict] = []

    def __call__(self, req, headers, endpoint):
        self.seen.append(req)
        if self.mode == "drop":
            return {**req, "messages": req["messages"][1:]}, headers, []
        if self.mode == "mutate":
            req["messages"].pop()
            return req, headers, []
        return req, headers, []

    def observe_usage(self, endpoint, usage, req):
        self.observed.append(req)


@pytest.mark.parametrize(("preserves", "mode", "serializations"), [
    (True, "same", 0), (True, "drop", 1), (False, "same", 2), (False, "drop", 2), (False, "mutate", 2)])
def test_a_preserving_transform_gets_no_copy_and_no_reference_serialization(
        recorder, monkeypatch, preserves, mode, serializations):
    calls = []
    real = proxy_mod.serialize_request
    monkeypatch.setattr(proxy_mod, "serialize_request", lambda obj: calls.append(1) or real(obj))
    spy = Spy(preserves, mode)
    proxy = ContextoProxy(recorder, MemoryStore(key=b"k" * 32), spy, observe_usage=spy.observe_usage)
    proxy.start()
    try:
        cli = request([user("one"), assistant("two"), user("three")])
        odd = json.dumps(cli, indent=1, ensure_ascii=True).encode()  # bytes the serializer would not write
        post(proxy, "/v1/messages", odd)
    finally:
        proxy.stop()
    sent = Recorder.bodies[0][1]
    assert len(calls) == serializations
    if mode == "drop":
        assert sent == real({**cli, "messages": cli["messages"][1:]})
    elif mode == "mutate":
        assert sent == real({**cli, "messages": cli["messages"][:-1]})
    else:
        assert sent == odd  # unchanged: the CLI's own bytes
    # observe_usage always sees the CLI's request as parsed, never a mutated one.
    assert spy.observed[0] == cli
    assert (spy.observed[0] is spy.seen[0]) is preserves


def test_the_engine_and_identity_declare_they_preserve_input():
    assert RewriteEngine(MemoryStore(key=b"k" * 32)).preserves_input is True
    assert proxy_mod.identity_transform.preserves_input is True


# No aliasing: the engine never mutates what it is given ------------------------------------------

def test_the_engine_never_mutates_its_request_or_the_one_observe_usage_sees():
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=CONFIG)
    for path, req, _ in scripted_session(16):
        frozen = serialize_request(req)
        upstream, _, _ = engine(req, {}, path)
        assert serialize_request(req) == frozen
        sent = serialize_request(upstream)
        if path == rw.MESSAGES:
            usage = {"input_tokens": 2, "cache_read_input_tokens": math.ceil(len(sent) / 2) - 2, "output_tokens": 5}
            engine.observe_usage(path, usage, req)
        assert serialize_request(req) == frozen
        assert serialize_request(upstream) == sent
    assert engine.states[next(iter(engine.states))].epoch > 3


# Equivalence: old paths against new, through the real proxy --------------------------------------

def cli_body(messages: list[dict], extra: dict | None = None) -> dict:
    """The CLI's request: cache_control on the newest message only, and a single text block on the
    newest user prompt that older positions send as a plain string (rule 10 must match both)."""
    out = []
    for i, m in enumerate(messages):
        m = copy.deepcopy(m)
        newest = i == len(messages) - 1
        content = m.get("content")
        if (m.get("role") == "user" and isinstance(content, list) and len(content) == 1
                and content[0].get("type") == "text" and not newest):
            m["content"] = content[0]["text"]
        if newest and isinstance(m.get("content"), list) and m["content"]:
            m["content"][-1]["cache_control"] = {"type": "ephemeral"}
        out.append(m)
    return request(out, **(extra or {}))


def scripted_session(turns: int, resync_at: int | None = 9):
    """(path, request, usage) in order: turn starts, 0 to 2 tool rounds with thinking, a count_tokens
    request every third turn, context blocks on some later turn starts, and at ``resync_at`` a locally
    compacted early message (a paged_changed resync)."""
    msgs = [user(f"prompt 0 {FILL}", context=True), system()]
    for t in range(turns):
        if t:
            msgs.append(user(f"prompt {t} {FILL}", context=t % 4 == 0))
        if resync_at is not None and t == resync_at:
            msgs[3] = assistant("compacted locally")
        if t % 3 == 2:
            yield rw.COUNT_TOKENS, cli_body(msgs), 0
        yield rw.MESSAGES, cli_body(msgs), 0
        for r in range(t % 3):
            msgs += [tool_use(f"{t}_{r}", think=f"t{t}r{r}"), tool_result(f"{t}_{r}", f"output {t} {r} {FILL}")]
            yield rw.MESSAGES, cli_body(msgs), 0
        msgs.append(assistant(f"answer {t} {FILL}", think=f"a{t}"))


def drive(upstream: str, preserving: bool, turns: int = 18, restart_after: int = 10) -> dict:
    """The scripted session through the proxy, restarted once (a new engine and proxy on the same store,
    which resumes the recorded epoch). Returns what must be identical between the old and new paths. The
    store has a fixed key, so page ids are comparable between runs."""
    store = MemoryStore(key=b"k" * 32)
    log = MemoryStore(key=b"k" * 32)
    Recorder.bodies = []
    script = list(scripted_session(turns))
    split = next(i for i, (path, req, _) in enumerate(script)
                 if path == rw.MESSAGES and rw.at_turn_start(req["messages"])
                 and sum(1 for m in req["messages"] if rw.is_turn_start(m)) == restart_after + 1)
    engines = []
    for part in (script[:split], script[split:]):
        engine = RewriteEngine(store, config=CONFIG)
        engines.append(engine)
        if preserving:
            transform: Any = engine
        else:
            def transform(req, headers, endpoint, engine=engine):  # no preserves_input: the copying path
                return engine(req, headers, endpoint)
        proxy = ContextoProxy(upstream, log, transform, observe_usage=engine.observe_usage)
        proxy.start()
        try:
            for path, req, _ in part:
                post(proxy, path, json.dumps(req, indent=1).encode())
        finally:
            proxy.stop()
    events = [{k: v for k, v in e.items() if k not in VOLATILE_KEYS} for e in log.events]

    def engine_view(engine: RewriteEngine) -> dict:
        private = {k: v for k, v in vars(engine).items() if k not in ("store", "estimator", "config")}
        return {"states": engine.states, "private": private, "estimator": vars(engine.estimator)}

    pages = {pid: store.get_page(pid) for pid in store.page_ids()}
    return {"bodies": list(Recorder.bodies), "events": events, "engines": [engine_view(e) for e in engines],
            "epoch_record": {k: v for k, v in (store.epoch_record() or {}).items() if k != "version"},
            "pages": pages, "window": store.window(), "retained": store.retained(), "room": store.turn_room()}


def test_old_and_new_paths_send_identical_bytes_and_reach_identical_state(recorder, monkeypatch):
    new = drive(recorder, preserving=True)
    with monkeypatch.context() as m:
        # The old paths: no per-request memo (each message hashed at every use, every scan repeated),
        # rule 10's hash computed by normalising first, and the proxy's copy-and-compare transform step.
        m.setattr(rw, "request_scope", lambda messages: contextlib.nullcontext())
        m.setattr(rw, "_message_hash", reference_hash)
        old = drive(recorder, preserving=False)
    names = [e.get("event") for e in new["events"]]
    inner = [i.get("event") for e in new["events"] for i in e.get("transform_events") or []]
    # The session exercises what it claims to.
    assert inner.count("rotation") >= 5 and "page_out" in inner
    assert "resync" in inner and "epoch_resumed" in inner and "count_tokens" in inner
    assert names.count("request") == len(new["bodies"])
    assert "transform_error" not in names and not any(e.get("transform_error") for e in new["events"])
    assert any(b"thinking omitted" in body or b'"signature"' in body for _, body in new["bodies"])
    unchanged = [body for _, body in new["bodies"] if body.startswith(b"{\n")]
    assert unchanged  # requests before the first rotation forward the CLI's own bytes
    assert new["engines"][1]["private"]["_resumed"] is True
    for key in ("bodies", "events", "engines", "epoch_record", "pages", "window", "retained", "room"):
        assert new[key] == old[key], key
