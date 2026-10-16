"""Responses API dialect tests (M2 A5). Item shapes come from codex-cli 0.154.0 with gpt-6-astra as
measured with zero-spend probes (spikes/m2/codex/); all content is synthetic."""

import copy

from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES, ResponsesDialect
from contexto.rewrite import MESSAGES, Config, RewriteEngine, rewrite
from contexto.store.memory import MemoryStore
from contexto.tokens import TokenEstimator

FILL = "synthetic filler text " * 90
CONFIG = Config(bound=6_000, headroom_floor=500, pin_budget=200, index_budget=200)


def dev(text):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def lead():
    """The measured first-request lead, before the user's first prompt."""
    return [
        {"type": "additional_tools", "id": "at_synthetic", "role": "developer",
         "tools": [{"type": "namespace", "name": "functions", "tools": []}]},
        dev("synthetic base instructions"),
        {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "<skills_instructions>synthetic</skills_instructions>"},
            {"type": "input_text", "text": "<permissions instructions>synthetic</permissions instructions>"}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>synthetic</environment_context>"}]},
    ]


def prompt(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def reasoning(i):
    return {"type": "reasoning", "id": f"rs_{i}", "summary": [], "encrypted_content": f"opaque-synthetic-{i}"}


def answer(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call(i):
    return {"type": "function_call", "call_id": f"call_{i}", "name": "exec", "arguments": "{}"}


def output(i, text="synthetic output"):
    return {"type": "function_call_output", "call_id": f"call_{i}", "output": text}


def history(turns):
    items = lead()
    for t in range(turns):
        items += [prompt(f"prompt {t} {FILL}"), reasoning(t), call(t), output(t, f"out {t} {FILL}"), answer(f"answer {t}")]
    return items


def request(items, **extra):
    return {"model": "gpt-6-astra", "stream": True, "store": False, "input": items,
            "prompt_cache_key": "synthetic-session", "include": ["reasoning.encrypted_content"], **extra}


def run(items, state=None, store=None, config=CONFIG):
    store = store if store is not None else MemoryStore(key=b"k" * 32)
    up, st, ev = rewrite(request(items), state, store, config, TokenEstimator(), RESPONSES)
    return up, st, ev, store


def names(events):
    return [e["event"] for e in events]


def test_turn_starts_lead_and_system_like_items():
    d = ResponsesDialect()
    items = lead() + [prompt("p0"), reasoning(0), answer("a0")]
    assert d.lead_count(items) == 4
    assert [d.is_turn_start(i) for i in items] == [False, False, False, False, True, False, False]
    assert all(d.is_system_like(i) for i in items[:4]) and not d.is_system_like(items[4])
    assert d.is_assistant(reasoning(1)) and d.is_assistant(call(1)) and d.is_assistant(answer("x"))
    assert not d.is_assistant(output(1))
    assert d.classify_system(items, 1) == "session_start"
    assert d.classify_system(items + [dev("later context")], 7) == "carried_text"


def test_first_request_passes_through_unchanged():
    items = lead() + [prompt("first prompt")]
    req = request(items)
    before = copy.deepcopy(req)
    up, st, ev = rewrite(req, None, MemoryStore(key=b"k" * 32), Config(), TokenEstimator(), RESPONSES)
    assert req == before and up["input"] == items and st.k == 0
    for field in ("model", "store", "stream", "prompt_cache_key", "include"):
        assert up[field] == req[field]


def test_rotation_keeps_lead_first_merges_preamble_and_drops_old_reasoning():
    items = history(8) + [prompt("new prompt")]
    up, st, ev, store = run(items)
    assert "rotation" in names(ev) and st.k > 4
    out = up["input"]
    assert out[:4] == lead()  # the lead stays first, unchanged
    first = out[4]
    assert first["role"] == "user" and first["content"][0]["type"] == "input_text"
    assert first["content"][1:] == items[st.k]["content"]  # preamble part, then the prompt
    assert not any(i.get("type") == "reasoning" for i in out)  # existed at rotation: dropped
    calls = {i["call_id"] for i in out if i.get("type") == "function_call"}
    outputs = {i["call_id"] for i in out if i.get("type") == "function_call_output"}
    assert calls == outputs  # turns are paged whole, so pairs stay together
    pages = "".join(canonical_bytes(p).decode() for p in store.pages.values())
    assert "environment_context" not in pages and "opaque-synthetic-0" in pages  # pages keep reasoning


def test_reasoning_after_rotation_passes_untouched_and_prefix_is_stable():
    store = MemoryStore(key=b"k" * 32)
    items = history(8) + [prompt("new prompt")]
    up1, st, _, _ = run(items, store=store)
    later = items + [reasoning(99), call(99), output(99)]
    up2, _, ev2, _ = run(later, state=st, store=store)
    assert "rotation" not in names(ev2)
    assert up2["input"][-3] is later[-3]  # added after the rotation: kept
    n = len(up1["input"])
    assert [canonical_bytes(i) for i in up2["input"][:n]] == [canonical_bytes(i) for i in up1["input"]]


def test_no_rotation_inside_a_tool_loop():
    items = history(10) + [prompt("go"), call(50), output(50, FILL * 3)]
    up, st, ev, store = run(items)
    assert "rotation" not in names(ev) and not store.pages and up["input"] == items
    assert ev[-1].get("over_bound") is True


def test_usage_reads_input_tokens_as_the_total():
    assert RESPONSES.usage_tokens({"input_tokens": 1_000, "input_tokens_details": {"cached_tokens": 900},
                                   "output_tokens": 5}) == 1_000


def test_engine_dispatches_responses_paths_with_separate_state():
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=CONFIG)
    items = lead() + [prompt("codex prompt")]
    for path in ("/responses", "/v1/responses"):
        up, headers, ev = engine(request(items), {"authorization": "x"}, path)
        assert up["input"] == items and headers == {"authorization": "x"} and ev
    anthropic = {"model": "m", "messages": [{"role": "user", "content": "claude prompt"}]}
    engine(anthropic, {}, MESSAGES)
    assert sorted(k.split(":")[0] for k in engine.states) == ["anthropic", "responses"]
    engine.observe_usage("/responses", {"input_tokens": 777, "input_tokens_details": {"cached_tokens": 700}},
                         request(items))
    responses_state = next(v for k, v in engine.states.items() if k.startswith("responses:"))
    assert responses_state.last_input_tokens == 777
    assert engine(request(items), {}, "/models") == (request(items), {}, [])
