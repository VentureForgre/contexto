"""Fast checks of the scale harness helpers (synthetic traffic, fake upstream SSE, summary math).

No network beyond 127.0.0.1 and no model calls; the scale run itself is not part of the test suite.
"""

from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contexto import rewrite  # noqa: E402
from contexto.proxy import SSEEventSplitter, StreamHold, UsageReader  # noqa: E402
from scale.proxy import summarize, synth  # noqa: E402
from scale.proxy.upstream import FakeUpstream, reply_events  # noqa: E402

SHAPE = synth.Shape(head_tokens=20_000, turn_tokens=8_000)


def test_wire_builder_matches_serialized_request():
    head = synth.head(SHAPE)
    wire = synth.WireBuilder(head)
    history = []
    for t in range(3):
        messages = synth.turn(SHAPE, t)
        for end, _ in synth.request_points(SHAPE, t, messages):
            assert wire.body(messages[:end]) == synth.serialize(synth.request(head, history, messages[:end]))
        wire.extend(messages)
        history.extend(messages)


def test_turns_are_deterministic_and_shaped_like_claude_code():
    assert synth.turn(SHAPE, 5) == synth.turn(SHAPE, 5)
    first = synth.turn(SHAPE, 0)
    assert first[0]["content"][0]["text"].startswith("<system-reminder>")
    assert first[1]["role"] == "system" and "output_config" in first[1]
    for t in range(12):
        messages = synth.turn(SHAPE, t)
        assert rewrite.is_turn_start(messages[0])
        assert sum(rewrite.is_turn_start(m) for m in messages) == 1
        assert messages[-1]["role"] == "assistant"
        points = synth.request_points(SHAPE, t, messages)
        tool_results = sum(1 for m in messages if m["role"] == "user" and not rewrite.is_turn_start(m))
        assert len(points) == 1 + tool_results
        assert [kind for _, kind in points] == ["tool_use"] * tool_results + ["end_turn"]


def test_mixed_reminders_only_on_later_turn_starts():
    mixed = synth.Shape(head_tokens=20_000, turn_tokens=8_000, reminders="mixed")
    later = [synth.turn(mixed, t)[0] for t in range(1, 60)]
    with_reminder = [m for m in later if any(rewrite.is_context_block(b) for b in m["content"])]
    assert 0 < len(with_reminder) < len(later)
    assert all(not any(rewrite.is_context_block(b) for b in synth.turn(SHAPE, t)[0]["content"]) for t in range(1, 20))


def test_head_size_tracks_the_requested_tokens():
    for tokens in (20_000, 50_000, 90_000):
        head = synth.head(synth.Shape(head_tokens=tokens))
        size = synth.tokens_of(len(synth.serialize({"system": head["system"], "tools": head["tools"]})))
        assert abs(size - tokens) / tokens < 0.05


def _parse(events: list[bytes]) -> tuple[UsageReader, StreamHold]:
    reader = UsageReader("text/event-stream")
    triggered = []
    hold = StreamHold("messages", lambda: triggered.append(True))
    splitter = SSEEventSplitter()
    for raw in splitter.feed(b"".join(events)):
        reader.feed_event(raw)
        hold.feed(raw)
    return reader, hold


def test_fake_sse_is_read_like_the_messages_api():
    reader, hold = _parse(reply_events("tool_use", 12_345, "x"))
    assert sum(reader.usage[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) == 12_345
    assert reader.usage["output_tokens"] == 180
    assert reader.ending["stop_reason"] == "tool_use"
    assert hold.triggered and hold.held_events > 0
    reader, hold = _parse(reply_events("end_turn", 10, "y"))
    assert reader.ending["stop_reason"] == "end_turn" and hold.held_events == 0


def test_fake_upstream_counts_the_forwarded_body():
    upstream = FakeUpstream()
    port = upstream.start()
    try:
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/messages", body=body, headers={"x-scale-id": "a1", "x-scale-reply": "end_turn",
                                                                   "Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
    finally:
        upstream.stop()
    assert response.status == 200 and b"message_stop" in data
    record = upstream.take("a1")
    assert record["bytes"] == len(body) and record["tokens"] == synth.tokens_of(len(body)) and record["messages"] == 1


def test_summary_statistics():
    assert summarize.p95([1.0, 2.0, 3.0, 4.0, 100.0]) == 100.0
    assert summarize.p95(list(map(float, range(101)))) == 95.0
    assert summarize.median_of_medians([[1.0, 9.0, 2.0], [5.0], []]) == 3.5
