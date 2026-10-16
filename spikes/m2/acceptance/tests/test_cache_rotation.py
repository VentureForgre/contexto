"""Cache-aware rotation replay (packet A28): the request rebuild, restarts, the cache model, and the
append-only index prototype, on synthetic data only."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import cache_rotation as cr  # noqa: E402
from contexto.rewrite import Config  # noqa: E402


def item(kind: str, role: str = "", text: str = "x", **extra) -> dict:
    payload = {"type": kind, cr.ROLLOUT_ONLY: {"meta": 1}, **extra}
    if kind == "message":
        payload.update(role=role, content=[{"type": "input_text" if role != "assistant" else "output_text", "text": text}])
    return {"type": "response_item", "payload": payload}


USAGE = {"type": "token_usage_record"}


def test_codex_inputs_are_the_items_before_each_requests_first_output():
    rollout = [item("message", "developer"), item("message", "user", "q1"), item("message", "assistant", "a1"), USAGE,
               item("message", "user", "q2"), item("custom_tool_call", call_id="c1", input="{}"), USAGE,
               item("custom_tool_call_output", call_id="c1", output="ok"), item("reasoning", summary=[]),
               item("message", "assistant", "a2"), USAGE]
    inputs = cr.codex_inputs(rollout)
    assert [len(i) for i in inputs] == [2, 4, 6]
    assert all(cr.ROLLOUT_ONLY not in it for request in inputs for it in request)
    assert [it["type"] for it in inputs[2]] == ["message", "message", "message", "message", "custom_tool_call",
                                                "custom_tool_call_output"]


def test_restarts_are_the_first_requests_after_a_proxy_start():
    def req():
        return {"event": "request", "path": "/responses", "usage": {}}
    log = [{"event": "proxy_start"}, req(), req(), {"event": "proxy_stop"}, {"event": "proxy_start"}, req(),
           {"event": "mcp_call"}, req(), {"event": "proxy_start"}, req(), {"event": "request", "path": "/models"}]
    assert cr.restarts(log, "/responses") == {2, 4}


def test_the_cache_view_puts_the_head_first_and_ignores_cache_markers():
    marked = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi",
                                                                       "cache_control": {"type": "ephemeral"}}]}],
              "system": [{"type": "text", "text": "sys"}], "tools": [{"name": "t"}]}
    plain = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "system": [{"type": "text", "text": "sys"}],
             "tools": [{"name": "t"}]}
    view, head = cr.cache_view("messages", marked)
    assert view == cr.cache_view("messages", plain)[0]
    assert view[:head] == cr.ser([[{"name": "t"}], [{"type": "text", "text": "sys"}]])
    rview, rhead = cr.cache_view("responses", {"instructions": "ins", "tools": None, "input": [{"type": "message"}]})
    assert rview[:rhead] == cr.ser(["ins", None])


def test_checkpoint_reads_the_longest_earlier_whole_prompt_else_the_head():
    first, head = cr.cache_view("responses", {"instructions": "head", "input": [{"a": 1}]})
    second, _ = cr.cache_view("responses", {"instructions": "head", "input": [{"a": 1}, {"b": 2}]})
    rotated, _ = cr.cache_view("responses", {"instructions": "head", "input": [{"c": 3}]})
    other_head, oh = cr.cache_view("responses", {"instructions": "HEAD", "input": [{"a": 1}]})
    earlier = [(first, head, 100)]
    assert cr.checkpoint_read(second, head, 150, 40, earlier) == 100  # appended: reads the earlier prompt
    assert cr.checkpoint_read(rotated, head, 120, 40, earlier) == 40  # rotated: the head only
    assert cr.checkpoint_read(other_head, oh, 120, 40, earlier) == 0  # a changed head: nothing
    assert cr.checkpoint_read(second, head, 90, 40, earlier) == 90  # never more than the request itself


def test_head_tokens_are_the_most_common_read_on_rotation_requests():
    def rot(read: int, rotated: bool = True) -> tuple[dict, dict]:
        events = [{"event": "rotation"}] if rotated else []
        return {"transform_events": events}, {"cached": read}
    pairs = [rot(9088), rot(9088), rot(0), rot(17000, rotated=False), rot(9100)]
    run = {"logged": [p[0] for p in pairs], "measured": [p[1] for p in pairs]}
    assert cr.head_tokens(run) == 9088


def test_the_oldest_first_index_appends_so_the_previous_index_is_a_prefix():
    pages = [{"id": f"pg_{i:016x}", "turn": i, "stub": {"user_text": f"turn {i}", "tools": [], "paths": []}}
             for i in range(6)]
    shorter = cr.oldest_first_index(pages[:4], 4_000)
    longer = cr.oldest_first_index(pages, 4_000)
    assert longer.startswith(shorter) and longer.count("\n") == 5
    clipped = cr.oldest_first_index(pages, 30)
    assert clipped.splitlines()[-1].endswith("newer pages omitted; use contexto_search to find them)")


def test_cost_applies_the_long_context_multipliers():
    rows = [{"tokens": 50_000, "cached": 40_000, "uncached": 10_000, "output": 100},
            {"tokens": 150_000, "cached": 100_000, "uncached": 50_000, "output": 100}]
    p = cr.PRICES["claude-haiku-5-5"]
    want = (40_000 * p["read"] + 10_000 * p["write"] + 100 * p["output"]
            + 5 * (100_000 * p["read"] + 50_000 * p["write"]) + 5 * 100 * p["output"]) / 1e6
    assert abs(cr.cost(rows, "claude-haiku-5-5") - want) < 1e-12


def synthetic_run(turns: int, restarts: set[int]) -> dict:
    """A Messages run of ``turns`` single-request turns, each turn about 6,000 tokens of text."""
    bodies, messages = [], []
    for t in range(turns):
        messages = messages + [{"role": "user", "content": f"turn {t} " + "word " * 4_000}]
        bodies.append({"model": "claude-haiku-5-5", "system": "sys", "tools": [], "messages": list(messages),
                       "stream": True})
        messages = messages + [{"role": "assistant", "content": [{"type": "text", "text": f"reply {t}"}]}]
    sizes = [len(cr.ser(b)) for b in bodies]
    measured = [{"input": s // 4, "cached": 0, "output": 10} for s in sizes]
    logged = [{"request_bytes": s, "original_request_bytes": s, "transform_events": []} for s in sizes]
    return {"api": "messages", "endpoint": "/v1/messages", "model": "claude-haiku-5-5", "bodies": bodies,
            "headers": [{}] * turns, "logged": logged, "measured": measured, "restarts": restarts,
            "rebuild_offsets": [0] * turns, "head_tokens": 10}


def test_a_restart_starts_a_new_engine_at_epoch_zero_unless_resumed():
    run = synthetic_run(12, restarts={10, 11})  # after the first rotation, when the full history is over the bound
    config = Config(bound=64_000, headroom_floor=10_000, min_turn_room=5_000)
    restarted = cr.replay(run, config)
    resumed = cr.replay(run, config, resume=True)
    assert len(restarted) == len(resumed) == 12
    # Each restart's engine has no epoch state, so it rotates from epoch 0 again; a resumed one goes on.
    assert [r["epoch"] for r in restarted[9:]] == [1, 1, 1]
    assert [r["epoch"] for r in resumed[9:]] == [1, 2, 3]
    assert all(r["rotations"] for r in restarted[9:])
    assert restarted[0]["tokens"] == run["measured"][0]["input"]  # the baseline's tokens are the measured ones


def test_rate_converts_byte_differences_from_the_baseline():
    run = synthetic_run(12, restarts=set())
    config = Config(bound=64_000, headroom_floor=10_000, min_turn_room=5_000)
    base = cr.replay(run, config)
    refs = [r["bytes"] for r in base]
    wide = Config(bound=200_000)  # no rotation: more bytes than the baseline after its first rotation
    default = cr.replay(run, wide, baseline_bytes=refs)
    halved = cr.replay(run, wide, baseline_bytes=refs, rate=cr.bytes_per_token(run) / 2)
    extra = [d["tokens"] - m["input"] for d, m in zip(default, run["measured"])]
    extra_halved = [h["tokens"] - m["input"] for h, m in zip(halved, run["measured"])]
    assert any(extra) and all(abs(b - 2 * a) <= 1 for a, b in zip(extra, extra_halved))
