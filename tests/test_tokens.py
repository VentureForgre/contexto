import hashlib
import math

import pytest

from contexto.canonical import canonical_bytes, canonical_json
from contexto.tokens import (
    EWMA_DECAY,
    TEXT_BYTES_PER_TOKEN,
    TOOL_RESULT_BYTES_PER_TOKEN,
    TokenEstimator,
    estimate_item_tokens,
    estimate_message_tokens,
    estimate_tokens,
    media_keys,
    message_bytes,
)


def test_canonical_json_is_sorted_compact_utf8():
    obj = {"b": 1, "a": ["é", {"d": None, "c": True}]}
    assert canonical_json(obj) == '{"a":["é",{"c":true,"d":null}],"b":1}'
    assert canonical_bytes(obj) == canonical_json(obj).encode("utf-8")


def test_seeds_are_1_5_for_tool_results_and_3_5_for_other_text():
    assert TOOL_RESULT_BYTES_PER_TOKEN == 1.5 and TEXT_BYTES_PER_TOKEN == 3.5
    assert estimate_tokens("x" * 300, tool_result=True) == 200
    assert estimate_tokens("x" * 350) == 100


def test_prose_observation_preserves_dense_tool_seed_until_tool_ratio_is_observed():
    tool = {"type": "tool_result", "tool_use_id": "t", "content": "1 2 3 " * 50}
    msgs = [{"role": "user", "content": [tool]}, {"role": "user", "content": "plain words " * 20}]
    tb, rb = message_bytes(msgs)
    est = TokenEstimator()
    assert est.estimate_messages(msgs) == math.ceil(tb / 3.5) + math.ceil(rb / 1.5)
    est.observe(request_bytes=500, input_tokens=200)  # measured prose 2.5 bytes per token
    # Prose uses measured 2.5; tool_result keeps the conservative 1.5 seed instead of being loosened to 2.5.
    assert est.estimate_messages(msgs) == math.ceil(tb / 2.5) + math.ceil(rb / 1.5)
    assert est.estimate("x" * 300, tool_result=True) == 200
    assert est.estimate("x" * 250) == 100
    # Once tool output itself is observed, tool_result uses its own measured ratio independently of prose.
    est.observe(request_bytes=400, input_tokens=200, tool_result=True)  # measured tool 2.0 bytes/token
    assert est.measured_bytes_per_token == 2.5 and est.measured_tool_bytes_per_token == 2.0
    assert est.estimate_messages(msgs) == math.ceil(tb / 2.5) + math.ceil(rb / 2.0)
    assert est.estimate("x" * 300, tool_result=True) == 150


def test_prose_ratio_below_tool_seed_tightens_tool_estimate():
    est = TokenEstimator()
    est.observe(request_bytes=120, input_tokens=100)  # very dense 1.2 bytes/token
    assert est.tool_bytes_per_token == pytest.approx(1.2)
    assert est.estimate("x" * 120, tool_result=True) == 100


def test_estimate_items_keeps_tool_output_separate_from_prose():
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello " * 40}]},
        {"type": "function_call_output", "call_id": "c1", "output": "12345 " * 60},
    ]
    est = TokenEstimator()
    est.observe(request_bytes=350, input_tokens=100)  # prose 3.5
    got = est.estimate_items(items)
    expected = (estimate_tokens(canonical_bytes([items[0]]), bytes_per_token=3.5)
                + estimate_tokens(canonical_bytes([items[1]]), bytes_per_token=1.5))
    assert got == expected
    assert estimate_item_tokens(items, 3.5, tool_bytes_per_token=2.0) == (
        estimate_tokens(canonical_bytes([items[0]]), bytes_per_token=3.5)
        + estimate_tokens(canonical_bytes([items[1]]), bytes_per_token=2.0)
    )


def test_default_ratios_round_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefg") == 2  # 7 bytes at 3.5
    assert estimate_tokens("abcdefgh") == 3  # 8 bytes at 3.5, rounded up
    assert estimate_tokens(b"abc", tool_result=True) == 2  # 3 bytes at 1.5
    assert estimate_tokens(b"abcd", tool_result=True) == 3  # 4 bytes at 1.5, rounded up
    assert estimate_tokens("é") == 1  # 2 UTF-8 bytes


def test_message_estimate_splits_tool_results_from_text():
    tool = {"type": "tool_result", "tool_use_id": "t", "content": "x" * 40}
    text = {"type": "text", "text": "y" * 40}
    msgs = [{"role": "user", "content": [tool]}, {"role": "assistant", "content": [text]},
            {"role": "user", "content": "z" * 7}]
    tb, rb = message_bytes(msgs)
    assert rb == len(canonical_bytes(tool)) and tb == len(canonical_bytes(text)) + 7
    assert estimate_message_tokens(msgs) == math.ceil(tb / 3.5) + math.ceil(rb / 1.5)


def test_estimator_switches_to_measured_ratio_with_ewma_decay():
    est = TokenEstimator()
    assert est.estimate("a" * 70) == 20
    est.observe(request_bytes=1000, input_tokens=500)  # measured 2.0 bytes per token
    assert est.measured_bytes_per_token == 2.0
    assert est.estimate("a" * 70) == 35
    msgs = [{"role": "user", "content": "a" * 70}]
    assert est.estimate_messages(msgs) == 35
    est.observe(request_bytes=0, input_tokens=10)  # ignored
    est.observe(request_bytes=100, input_tokens=float("nan"))  # ignored
    est.observe(request_bytes=100, input_tokens=float("inf"))  # ignored
    assert est.measured_bytes_per_token == 2.0
    est.observe(request_bytes=3000, input_tokens=500)  # EWMA: newer 6.0 outweighs decayed 2.0
    expected = (1000 * EWMA_DECAY + 3000) / (500 * EWMA_DECAY + 500)
    assert est.measured_bytes_per_token == pytest.approx(expected)
    assert est.measured_bytes_per_token > 4.0


def test_ewma_adapts_quickly_after_long_prose_phase():
    est = TokenEstimator()
    for _ in range(50):
        est.observe(request_bytes=35_000, input_tokens=10_000)  # 3.5 bytes/token prose
    assert est.measured_bytes_per_token == pytest.approx(3.5)
    for _ in range(15):
        est.observe(request_bytes=18_000, input_tokens=10_000)  # shift to 1.8 bytes/token dense logs
    # Unbounded cumulative would stay > 3.1; EWMA adapts below 2.0 within 15 turns.
    assert est.measured_bytes_per_token < 2.0


def test_media_keys_use_sha256_digests():
    payload = "AAAA" * 16
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}}
    url_block = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    keys = media_keys([block, url_block])
    assert keys[0] == (len(payload), hashlib.sha256(payload.encode()).digest())
    raw = canonical_bytes(url_block)
    assert keys[1] == (len(raw), hashlib.sha256(raw).digest())
