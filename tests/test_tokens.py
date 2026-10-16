from contexto.canonical import canonical_bytes, canonical_json
from contexto.tokens import TokenEstimator, estimate_message_tokens, estimate_tokens, message_bytes


def test_canonical_json_is_sorted_compact_utf8():
    obj = {"b": 1, "a": ["é", {"d": None, "c": True}]}
    assert canonical_json(obj) == '{"a":["é",{"c":true,"d":null}],"b":1}'
    assert canonical_bytes(obj) == canonical_json(obj).encode("utf-8")


def test_default_ratios_round_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdefg") == 2  # 7 bytes at 3.5
    assert estimate_tokens("abcdefgh") == 3  # 8 bytes at 3.5, rounded up
    assert estimate_tokens(b"abcd", tool_result=True) == 2  # 4 bytes at 2.0
    assert estimate_tokens("é") == 1  # 2 UTF-8 bytes


def test_message_estimate_splits_tool_results_from_text():
    tool = {"type": "tool_result", "tool_use_id": "t", "content": "x" * 40}
    text = {"type": "text", "text": "y" * 40}
    msgs = [{"role": "user", "content": [tool]}, {"role": "assistant", "content": [text]},
            {"role": "user", "content": "z" * 7}]
    tb, rb = message_bytes(msgs)
    assert rb == len(canonical_bytes(tool)) and tb == len(canonical_bytes(text)) + 7
    assert estimate_message_tokens(msgs) == -(-tb // 3.5) + -(-rb // 2.0)


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
