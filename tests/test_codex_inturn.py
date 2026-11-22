"""In-turn paging (ResponsesInTurn) for Codex / OpenAI Responses API."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from contexto import inturn
from contexto.codex import RESPONSES
from contexto.rewrite import Config, EpochState, RewriteEngine, rewrite
from contexto.store.memory import MemoryStore
from contexto.store.pages import _result_locator, build_stub
from contexto.store.render import render_page
from contexto.tokens import TokenEstimator

FILL = "synthetic codex tool output line " * 120


def lead() -> list[dict]:
    return [
        {"type": "additional_tools", "id": "at_1", "role": "developer", "tools": []},
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "base instructions"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text",
                                                         "text": "<environment_context>cwd</environment_context>"}]},
    ]


def user_prompt(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def assistant_text(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def reasoning(i: int, summary: str = "") -> dict:
    item: dict = {"type": "reasoning", "id": f"rs_{i}", "summary": [], "encrypted_content": f"enc_{i}"}
    if summary:
        item["summary"] = [{"type": "summary_text", "text": summary}]
    return item


def fn_call(i: int, name: str = "exec_command", args: dict | None = None) -> dict:
    return {"type": "function_call", "call_id": f"call_{i}", "name": name,
            "arguments": json.dumps(args or {"cmd": f"cat src/mod_{i}.py"})}


def fn_output(i: int, text: str) -> dict:
    return {"type": "function_call_output", "call_id": f"call_{i}", "output": text}


def custom_call(i: int, name: str = "exec", code: str = "print(1)") -> dict:
    return {"type": "custom_tool_call", "call_id": f"custom_{i}", "name": name, "input": code}


def custom_output(i: int, output: str | list[dict]) -> dict:
    return {"type": "custom_tool_call_output", "call_id": f"custom_{i}", "output": output}


def local_shell_call(i: int, command: list[str]) -> dict:
    return {"type": "local_shell_call", "call_id": f"sh_{i}", "action": {"type": "exec", "command": command}}


def local_shell_output(i: int, text: str) -> dict:
    return {"type": "local_shell_call_output", "call_id": f"sh_{i}", "output": text}


def web_search_call(i: int, query: str) -> dict:
    return {"type": "web_search_call", "id": f"ws_{i}", "status": "completed", "action": {"query": query}}


def req(items: list[dict], **extra) -> dict:
    return {"model": "gpt-6-astra", "stream": True, "store": False, "input": items, **extra}


def test_responses_rounds_cover_function_custom_shell_and_hosted_calls():
    hooks = inturn.RESPONSES
    items = [
        *lead(),
        user_prompt("inspect everything"),
        reasoning(0),
        fn_call(0),
        fn_output(0, "out0"),
        reasoning(1),
        custom_call(1, "apply_patch", "*** Begin Patch"),
        custom_output(1, [{"type": "input_text", "text": "patched"}]),
        local_shell_call(2, ["git", "status"]),
        local_shell_output(2, "clean"),
        web_search_call(3, "rfc 6455"),
        reasoning(4),
        fn_call(4, "read_file", {"file_path": "README.md"}),
        fn_output(4, "readme"),
    ]
    start = len(lead())
    rounds = hooks.rounds(items, start, len(items))
    assert len(rounds) == 5
    # Round 0: reasoning(0) + fn_call(0) + fn_output(0)
    assert [items[i]["type"] for i in rounds[0].removed] == ["reasoning", "function_call", "function_call_output"]
    # Round 1: reasoning(1) + custom_call(1) + custom_output(1)
    assert [items[i]["type"] for i in rounds[1].removed] == [
        "reasoning", "custom_tool_call", "custom_tool_call_output"]
    # Round 2: local_shell_call(2) + local_shell_output(2)
    assert [items[i]["type"] for i in rounds[2].removed] == ["local_shell_call", "local_shell_call_output"]
    # Round 3: hosted web_search_call(3) without an output item
    assert [items[i]["type"] for i in rounds[3].removed] == ["web_search_call"]
    # Round 4: reasoning(4) + fn_call(4) + fn_output(4)
    assert [items[i]["type"] for i in rounds[4].removed] == ["reasoning", "function_call", "function_call_output"]
    assert all(r.foldable for r in rounds)


def test_responses_results_and_stubbing_preserve_call_id_and_non_text_parts():
    hooks = inturn.RESPONSES
    items = [
        user_prompt("check"),
        fn_call(0, "exec_command", {"cmd": "cat src/a.py"}),
        fn_output(0, "line\n" * 400),
        custom_call(1, "exec", "tools.read()"),
        custom_output(1, [
            {"type": "input_text", "text": "big text " * 300},
            {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 4000},
            {"type": "input_file", "filename": "report.pdf", "file_data": "B" * 4000},
        ]),
    ]
    assert hooks.results(items[2]) == [(0, "call_0")]
    assert hooks.results(items[4]) == [(0, "custom_1")]
    sz0 = hooks.size(items[2])
    sz1 = hooks.size(items[4])
    assert sz0.bytes > 1000 and sz0.images == 0 and sz0.documents == 0
    assert sz1.images == 1 and sz1.documents == 1
    assert hooks.has_media(items[4]) and hooks.has_document(items[4])
    assert hooks.stubbable(items[2]) and hooks.stubbable(items[4])

    stubbed_str = hooks.with_stubs(items[2], {0: "stub text"})
    assert stubbed_str == {"type": "function_call_output", "call_id": "call_0", "output": "stub text"}
    stubbed_list = hooks.with_stubs(items[4], {0: "stub list"})
    assert stubbed_list == {"type": "custom_tool_call_output", "call_id": "custom_1", "output": "stub list"}
    assert hooks.result_call_id(items[2]) == "call_0"
    assert hooks.result_call_id(items[4]) == "custom_1"


def test_responses_describe_and_result_locator():
    hooks = inturn.RESPONSES
    name, loc, _ = hooks.describe(fn_call(0, "exec_command", {"cmd": "pytest -q"}))
    assert name == "exec_command" and loc == "cmd: pytest -q"
    name, loc, _ = hooks.describe(local_shell_call(2, ["git", "diff", "--stat"]))
    assert name == "local_shell_call" and loc == "command: git diff --stat"
    name, loc, _ = hooks.describe(fn_call(3, "grep_files", {"pattern": "ResponsesInTurn", "path": "src/contexto"}))
    assert name == "grep_files" and loc == "pattern: ResponsesInTurn (in src/contexto)"
    # Result pages carry the call + output so _result_locator extracts the locator for stubs/index lines.
    page_items = hooks.result_page(fn_call(0, "exec_command", {"cmd": "cat src/a.py"}), fn_output(0, "body"))
    assert _result_locator(page_items) == "exec_command cmd: cat src/a.py"
    stub = build_stub(page_items)
    assert stub["tools"] == ["exec_command"]


def test_responses_thinking_shape_counts_reasoning_items():
    items = [*lead(), user_prompt("go"), reasoning(0), fn_call(0), fn_output(0, "x"), reasoning(1), fn_call(1),
             fn_output(1, "y")]
    state = EpochState(k=len(lead()), strip_upto=len(lead()) + 2)
    assert inturn.thinking_shape(items, state, RESPONSES) == {
        "kept_before": 1, "stripped": 1, "kept_between": 0, "kept_after": 0,
    }


def test_long_codex_tool_loop_stays_under_bound_and_pages_are_recallable():
    store = MemoryStore(key=b"k" * 32)
    config = Config(bound=8_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=1_000)
    engine = RewriteEngine(store, config=config)
    items = [*lead(), user_prompt("inspect every module in the repo")]
    batches = 0
    for r in range(35):
        items.append(reasoning(r))
        if r % 4 == 1:
            items.append(custom_call(r, "exec", f"tools.read({r})"))
            items.append(custom_output(r, [{"type": "input_text", "text": f"custom_marker_{r} {FILL}"}]))
        elif r % 4 == 2:
            items.append(local_shell_call(r, ["cat", f"mod_{r}.py"]))
            items.append(local_shell_output(r, f"shell_marker_{r} {FILL}"))
        elif r % 4 == 3:
            items.append(web_search_call(r, f"query {r}"))
            items.append(fn_call(r, "exec_command", {"cmd": f"echo {r}"}))
            items.append(fn_output(r, f"fn_marker_{r} {FILL}"))
        else:
            items.append(fn_call(r, "exec_command", {"cmd": f"cat src/mod_{r}.py"}))
            items.append(fn_output(r, f"fn_marker_{r} {FILL}"))

        up, _, events = engine(req(items), {}, "/v1/responses")
        req_ev = events[-1]
        assert req_ev["event"] == "request"
        est = req_ev.get("sent_estimate", req_ev.get("estimate", 0))
        assert est <= config.bound, (r, req_ev)
        assert not req_ev.get("over_bound"), (r, req_ev)
        if any(e["event"] == "in_turn_batch" for e in events):
            batches += 1
        # Simulate upstream reporting input_tokens close to the estimator.
        engine.observe_usage("/v1/responses", {"input_tokens": est}, req(items),
                             tool_calls=True, tool_call_ids=(f"call_{r}",))

    assert batches >= 2
    assert store.pages
    # Fold note is placed as an input_text block on the turn-start user message.
    turn_start = up["input"][len(lead())]
    assert turn_start["role"] == "user"
    notes = [p["text"] for p in turn_start["content"] if p.get("type") == "input_text" and inturn.note_page(p["text"])]
    assert len(notes) == 1 and "Contexto folded" in notes[0]
    page_id = inturn.note_page(notes[0])
    assert page_id is not None and store.get_page(page_id) is not None
    # Folded content is searchable and rendered cleanly.
    hits = store.search("fn_marker_0")
    assert hits and "fn_marker_0" in render_page(store.get_page(hits[0]))
    # No orphaned outputs without their paired call.
    kept_calls = {i["call_id"] for i in up["input"] if str(i.get("type", "")).endswith("_call") and "call_id" in i}
    kept_outs = {i["call_id"] for i in up["input"] if str(i.get("type", "")).endswith("_output") and "call_id" in i}
    assert kept_outs <= kept_calls


def test_codex_subagent_tool_loop_pages_in_turn_independently():
    """A Codex subagent (separate prompt, not main) also uses ResponsesInTurn and stays under bound."""
    store = MemoryStore(key=b"k" * 32)
    config = Config(bound=7_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=800)
    engine = RewriteEngine(store, config=config)
    # Main conversation first.
    engine(req([*lead(), user_prompt("main task")]), {}, "/v1/responses")
    # Subagent starts a long tool loop.
    sub_items = [*lead(), user_prompt("subagent task: scan files")]
    sub_batches = 0
    for r in range(20):
        sub_items += [reasoning(r), fn_call(r, "exec_command", {"cmd": f"cat sub_{r}.py"}),
                      fn_output(r, f"sub_marker_{r} {FILL}")]
        up, _, events = engine(req(sub_items), {}, "/v1/responses")
        assert events[-1]["main"] is False
        est = events[-1].get("sent_estimate", events[-1].get("estimate", 0))
        assert est <= config.bound
        if any(e["event"] == "in_turn_batch" for e in events):
            sub_batches += 1
    assert sub_batches >= 1
    assert len(up["input"]) < len(sub_items)


def test_responses_resync_clips_mid_round_cleanly():
    store = MemoryStore(key=b"k" * 32)
    config = Config(bound=7_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=800)
    items = [*lead(), user_prompt("loop")]
    state = EpochState()
    for r in range(15):
        items += [reasoning(r), fn_call(r), fn_output(r, f"m{r} {FILL}")]
        _, state, _ = rewrite(req(items), state, store, config, TokenEstimator(), RESPONSES)
    assert state.inturn.folds
    # Cut right in the middle of the first folded span (after reasoning, before output).
    a, b = state.inturn.folds[0][1][0]
    mid = a + 1
    truncated = items[:mid] + [assistant_text("changed")]
    clipped = inturn.clipped(state.inturn, mid, truncated, RESPONSES)
    # The cut round is dropped cleanly, never leaving a half-round fold.
    for _, spans, _ in clipped.folds:
        for _sa, sb in spans:
            assert sb <= mid
    viewed = inturn.view(truncated, replace(state, inturn=clipped), RESPONSES)
    assert viewed[-1] == assistant_text("changed")


@pytest.mark.parametrize("note_bytes", [900])
def test_responses_folds_and_index_pages_work_when_note_cap_is_exceeded(monkeypatch, note_bytes):
    monkeypatch.setattr(inturn, "NOTE_BYTES", note_bytes)
    store = MemoryStore(key=b"k" * 32)
    config = Config(bound=6_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=500)
    engine = RewriteEngine(store, config=config)
    items = [*lead(), user_prompt("many small folds")]
    for r in range(40):
        items += [reasoning(r), fn_call(r, "exec_command", {"cmd": f"cat src/module_{r:04d}.py"}),
                  fn_output(r, f"small-{r} " + "x" * 1800)]
        up, _, events = engine(req(items), {}, "/v1/responses")
        est = events[-1].get("sent_estimate", events[-1].get("estimate", 0))
        engine.observe_usage("/v1/responses", {"input_tokens": est}, req(items),
                             tool_calls=True, tool_call_ids=(f"call_{r}",))
    state = next(iter(engine.states.values()))
    assert len(state.inturn.folds) >= 3 and state.inturn.lists
    listing_id = state.inturn.lists[0][2]
    page = store.get_page(listing_id)
    assert page is not None and page["kind"] == inturn.FOLDS_PAGE
    # Fold note stays under NOTE_BYTES and names the folds page.
    note_text = up["input"][len(lead())]["content"][-1]["text"]
    assert inturn.note_size(note_text) <= note_bytes
    assert listing_id in note_text
