import math

from contexto.canonical import canonical_bytes, canonical_json
from contexto.tokens import (
    TEXT_BYTES_PER_TOKEN,
    TOOL_RESULT_BYTES_PER_TOKEN,
    TokenEstimator,
    estimate_message_tokens,
    estimate_tokens,
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


def test_measured_ratio_replaces_both_seeds_for_mixed_content():
    tool = {"type": "tool_result", "tool_use_id": "t", "content": "1 2 3 " * 50}
    msgs = [{"role": "user", "content": [tool]}, {"role": "user", "content": "plain words " * 20}]
    tb, rb = message_bytes(msgs)
    est = TokenEstimator()
    assert est.estimate_messages(msgs) == math.ceil(tb / 3.5) + math.ceil(rb / 1.5)
    est.observe(request_bytes=500, input_tokens=200)  # measured 2.5 bytes per token
    assert est.estimate_messages(msgs) == math.ceil((tb + rb) / 2.5)
    assert est.estimate("x" * 300, tool_result=True) == 120  # seed no longer used


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


def test_estimator_switches_to_measured_ratio():
    est = TokenEstimator()
    assert est.estimate("a" * 70) == 20
    est.observe(request_bytes=1000, input_tokens=500)  # measured 2.0 bytes per token
    assert est.measured_bytes_per_token == 2.0
    assert est.estimate("a" * 70) == 35
    msgs = [{"role": "user", "content": "a" * 70}]
    assert est.estimate_messages(msgs) == 35
    est.observe(request_bytes=0, input_tokens=10)  # ignored
    assert est.measured_bytes_per_token == 2.0
    est.observe(request_bytes=3000, input_tokens=500)  # cumulative: 4000 bytes over 1000 tokens
    assert est.measured_bytes_per_token == 4.0
