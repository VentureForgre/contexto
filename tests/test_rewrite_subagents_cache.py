"""Regression and unit tests for forked/twin subagent isolation, thinking-block prefix-binding
safety, in-turn PDF/cache fixes, minor rewrite fixes, active Anthropic cache_control breakpoint
management, stable prefix ordering across rotations, and TTL-aware cold-cache rotation."""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import replace

import pytest

from contexto import inturn, tokens
from contexto.bench.replay.upstream import _check_breakpoints
from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES
from contexto.rewrite import (
    ANTHROPIC,
    CLAUDE_CODE_COMPACTION_SUMMARY,
    COUNT_TOKENS,
    MAX_BREAKPOINTS,
    MAX_SIDE_STATES,
    MESSAGES,
    SIDE_STATE_TTL_S,
    Config,
    EpochState,
    RewriteEngine,
    _count_blocks_bp,
    _count_message_bp,
    _effective_cache_ttl,
    compaction_summary,
    is_turn_start,
    keyed,
    rewrite,
    strip_carried,
)
from contexto.store import SessionStore, check_epoch, check_room_id
from contexto.store.memory import MemoryStore
from contexto.tokens import PDF_MAX_BASE64, TokenEstimator
from inturn_harness import Cli, Upstream, blocks, output
from test_carried_bound import (
    FIXTURES,
    RESUME_CONFIG,
    placeholder_pages,
    session_5x,
    turn_5x,
)
from test_carried_bound import request as request_5x
from test_clear_main import NEW, OLD, headers, main_request, side_request
from test_epoch_resume import EpochStore
from test_project_start import ROOMY, later_session, main_close
from test_rewrite import FILL, assistant, conversation, history, request, run, system, tool_result, tool_use, user
from test_subagent_room import Clock


def total_breakpoints(body: dict) -> int:
    return (_count_blocks_bp(body.get("tools")) + _count_blocks_bp(body.get("system"))
            + sum(_count_message_bp(m) for m in body.get("messages") or []))


def ctx_kind(first_line: str, body: str = "synthetic content") -> dict:
    return {"type": "text", "text": f"<system-reminder>\n{first_line}\n{body}\n</system-reminder>"}


# 1. Forked & twin subagent isolation + side-state eviction ----------------------------------------


@pytest.mark.parametrize("extra_headers", [
    {"X-Claude-Code-Agent-Id": "agent-fork-1"},
    {"X-Claude-Code-Agent-Id": "agent-fork-1", "x-claude-code-request-class": "subagent"},
    {"X-Claude-Code-Agent-Id": "agent-fork-1", "x-claude-code-request-class": "main"},
    {"x-claude-code-request-class": "subagent"},
    {"x-claude-code-request-class": "auxiliary"},
    {"x-claude-code-request-class": "workflow"},
])
def test_forked_subagent_never_collides_with_main_state_or_store(tmp_path, extra_headers):
    """A forked subagent (or forked side request) sharing main's exact messages[0] must never
    overwrite states[main], retained.json, turn_room, window, or epoch.json."""
    store = SessionStore(tmp_path, "s1")
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    engine = RewriteEngine(store, config=cfg)
    main_msgs = history(6) + [user("main prompt")]
    main_req = main_request(main_msgs, OLD)
    main_hdrs = {**headers(OLD), "x-claude-code-request-class": "main"}

    engine(main_req, main_hdrs, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 2_000, "output_tokens": 100}, main_req,
                         tool_calls=True, headers=main_hdrs)

    main_key = engine._main
    main_state_before = engine.states[main_key]
    retained_before = store.retained_current()
    room_before = store.turn_room()
    epoch_before = store.epoch_record()
    window_before = store.window()

    fork_msgs = [*main_msgs, assistant("forking"), user("subagent forked task")]
    fork_req = main_request(fork_msgs, OLD)
    fork_hdrs = {**headers(OLD), **extra_headers}
    _, _, fork_events = engine(fork_req, fork_hdrs, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 3_200, "output_tokens": 150}, fork_req,
                         tool_calls=True, tool_call_ids=("toolu_fork_1",), headers=fork_hdrs)

    assert fork_events[-1]["main"] is False
    assert engine._main == main_key
    assert len(engine.states) == 2
    assert engine.states[main_key] == main_state_before
    assert store.retained_current() == retained_before
    assert store.turn_room() == room_before
    assert store.epoch_record() == epoch_before
    assert store.window() == window_before


def test_count_tokens_with_auxiliary_class_still_matches_main_state():
    """Claude Code tags count_tokens with x-claude-code-request-class: auxiliary even for main."""
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    engine = RewriteEngine(store, config=cfg)
    msgs = history(6) + [user("main prompt")]
    req = main_request(msgs, OLD)
    up, _, _ = engine(req, {**headers(OLD), "x-claude-code-request-class": "main"}, MESSAGES)
    counted, _, ev = engine(req, {**headers(OLD), "x-claude-code-request-class": "auxiliary"}, COUNT_TOKENS)
    assert ev[-1]["epoch"] == engine.states[engine._main].epoch >= 1
    assert canonical_bytes(counted["messages"]) == canonical_bytes(up["messages"])


def test_parallel_twin_subagents_with_identical_prompts_never_thrash():
    """Two parallel subagents with identical prompts and distinct X-Claude-Code-Agent-Id headers
    maintain completely isolated EpochStates with zero resyncs or unattributed usage."""
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=60_000, headroom_floor=8_000, min_turn_room=4_000)
    engine = RewriteEngine(store, config=cfg)

    main_req = main_request(conversation("main work", 2), OLD)
    engine(main_req, headers(OLD), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 5_000, "output_tokens": 100}, main_req,
                         tool_calls=True, headers=headers(OLD))

    h1 = {**headers(OLD), "X-Claude-Code-Agent-Id": "twin-alpha", "x-claude-code-request-class": "subagent"}
    h2 = {**headers(OLD), "X-Claude-Code-Agent-Id": "twin-beta", "x-claude-code-request-class": "subagent"}
    m1 = [user("Audit every module carefully")]
    m2 = [user("Audit every module carefully")]

    for step in range(4):
        r1 = main_request(m1, OLD)
        _, _, ev1 = engine(r1, h1, MESSAGES)
        uev1 = engine.observe_usage(MESSAGES, {"input_tokens": 8_000 + step * 3_000, "output_tokens": 100},
                                    r1, tool_calls=True, tool_call_ids=(f"t1_{step}",), headers=h1)
        r2 = main_request(m2, OLD)
        _, _, ev2 = engine(r2, h2, MESSAGES)
        uev2 = engine.observe_usage(MESSAGES, {"input_tokens": 6_000 + step * 1_000, "output_tokens": 100},
                                    r2, tool_calls=True, tool_call_ids=(f"t2_{step}",), headers=h2)
        assert not [e for e in ev1 + ev2 if e["event"] == "resync"]
        assert not [e for e in uev1 + uev2 if e["event"] == "usage_unattributed"]
        m1 = [*m1, tool_use(100 + step), tool_result(100 + step, f"alpha-output-{step} " * 50)]
        m2 = [*m2, tool_use(200 + step), tool_result(200 + step, f"beta-output-{step} " * 20)]

    k1 = keyed(ANTHROPIC, main_request(m1, OLD), h1)
    k2 = keyed(ANTHROPIC, main_request(m2, OLD), h2)
    assert k1 != k2 and k1 in engine.states and k2 in engine.states
    assert engine.states[k1].last_input_tokens != engine.states[k2].last_input_tokens


def test_clear_with_identical_first_prompt_isolates_by_session_id():
    """Typing the exact same opening prompt after /clear (new X-Claude-Code-Session-Id) must not
    collide with the old session's state."""
    store = MemoryStore(key=b"k" * 32)
    engine = RewriteEngine(store, config=Config(bound=100_000))
    old_req = main_request([user("hello")], OLD)
    engine(old_req, headers(OLD), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 500, "output_tokens": 50}, old_req,
                         tool_calls=False, headers=headers(OLD))
    old_main = engine._main

    new_req = main_request([user("hello")], NEW)
    _, _, events = engine(new_req, headers(NEW), MESSAGES)
    assert engine._main != old_main
    assert {"event": "main_changed", "reason": "new_cli_session", "turn_starts": 1} in events
    assert len(engine.states) == 2


def test_eviction_bounds_finished_and_idle_side_states():
    """Finished and TTL-expired side conversations are evicted from states and all auxiliary dicts,
    while main and conversations with live side rooms are preserved."""
    store = MemoryStore(key=b"k" * 32)
    store.clock = clock = Clock(1_000.0)
    engine = RewriteEngine(store, config=Config(bound=100_000))

    main_req = main_request(conversation("main session", 2), OLD)
    engine(main_req, headers(OLD), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 2_000, "output_tokens": 100}, main_req,
                         tool_calls=False, headers=headers(OLD))
    main_key = engine._main

    # Open a subagent with a live side room (tool_calls=True).
    live_hdrs = {**headers(OLD), "X-Claude-Code-Agent-Id": "live-agent"}
    live_req = main_request(conversation("live subagent", 1), OLD)
    engine(live_req, live_hdrs, MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 3_000, "output_tokens": 100}, live_req,
                         tool_calls=True, tool_call_ids=("toolu_live",), headers=live_hdrs)
    live_key = keyed(ANTHROPIC, live_req, live_hdrs)

    # Flood with MAX_SIDE_STATES + 40 one-shot finished side requests.
    for i in range(MAX_SIDE_STATES + 40):
        clock.now += 1.0
        sreq = side_request(f"title {i}", OLD)
        shdrs = {**headers(OLD), "x-claude-code-request-class": "auxiliary"}
        engine(sreq, shdrs, MESSAGES)
        engine.observe_usage(MESSAGES, {"input_tokens": 200, "output_tokens": 20}, sreq,
                             tool_calls=False, headers=shdrs)

    assert main_key in engine.states
    assert live_key in engine.states
    assert len(engine.states) <= MAX_SIDE_STATES + 2  # main + live_key + cap
    # Auxiliary dicts must stay in sync (no leaked keys outside engine.states).
    for aux in (engine._turn_starts, engine._cli_sessions, engine._classes, engine._idle,
                engine._turn_numbers, engine._agent_header, engine._last_activity,
                engine._last_response_monotonic):
        assert set(aux) <= set(engine.states)

    # Advance clock past SIDE_STATE_TTL_S and send a main request -> idle side entries AND expired
    # side-room owners are both evicted.
    clock.now += SIDE_STATE_TTL_S + 5.0
    main_next = main_request(conversation("main session", 3), OLD)
    engine(main_next, headers(OLD), MESSAGES)
    assert main_key in engine.states
    assert live_key not in engine.states
    assert not engine._side_rooms
    assert set(engine.states) == {main_key}


def test_scoped_keys_satisfy_store_validation_and_survive_clear_restart_and_eviction(tmp_path):
    """Hashed scoped keys pass check_epoch() and check_room_id() even with hostile/long agent_ids,
    survive base-key eviction and empty headers, and resume cleanly after a restart."""
    store = SessionStore(tmp_path, "s_scoped")
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    engine = RewriteEngine(store, config=cfg)

    # 1. Hostile 250-char agent_id containing tab/control chars must produce a valid side room_id.
    hostile_hdrs = {**headers(OLD), "X-Claude-Code-Agent-Id": "a" * 240 + "\t\nagent"}
    main_req = main_request([user("hello")], OLD)
    engine(main_req, headers(OLD), MESSAGES)
    engine.observe_usage(MESSAGES, {"input_tokens": 500, "output_tokens": 50}, main_req,
                         tool_calls=False, headers=headers(OLD))
    old_main = engine._main

    sub_req = main_request([user("hello"), assistant("fork"), user("sub")], OLD)
    engine(sub_req, hostile_hdrs, MESSAGES)
    sub_events = engine.observe_usage(MESSAGES, {"input_tokens": 600, "output_tokens": 50}, sub_req,
                                      tool_calls=True, tool_call_ids=("t_hostile",), headers=hostile_hdrs)
    assert not any(e["event"] == "store_error" for e in sub_events)
    assert engine._side_rooms
    for room_id in engine._side_rooms:
        assert check_room_id(room_id) == room_id

    # 2. /clear with identical opening prompt writes a valid epoch.json (no epoch_record_stale!).
    new_msgs = [user("hello")]
    new_req = main_request(new_msgs, NEW)
    _, _, clear_events = engine(new_req, headers(NEW), MESSAGES)
    assert not any(e["event"] == "epoch_record_stale" for e in clear_events)
    new_main = engine._main
    assert new_main != old_main
    record = store.epoch_record()
    assert record is not None and record["conversation"] == new_main
    check_epoch(record)

    # 3. Dropping base key (old_main) + passing empty headers={} (session_id in metadata.user_id)
    #    must still resolve to new_main without splitting its EpochState.
    engine._drop_conversation(old_main)
    new_msgs2 = [*new_msgs, assistant("hi"), user("turn 2")]
    new_req2 = main_request(new_msgs2, NEW)
    _, _, ev2 = engine(new_req2, {}, MESSAGES)
    assert engine._main == new_main
    assert not any(e["event"] == "resync" for e in ev2)

    # 4. Restarting the engine on the same SessionStore resumes new_main from epoch.json!
    restarted = RewriteEngine(store, config=cfg)
    new_msgs3 = [*new_msgs2, assistant("hi 2"), user("turn 3")]
    _, _, ev3 = restarted(main_request(new_msgs3, NEW), headers(NEW), MESSAGES)
    assert any(e["event"] == "epoch_resumed" for e in ev3)
    assert restarted._main == new_main


# 2. Thinking-block prefix-binding safety ----------------------------------------------------------


def test_refresh_start_text_advances_strip_upto_and_strips_thinking(tmp_path):
    """When _refresh_start_text() mutates messages[0] at k == 0, every existing message's thinking
    must be stripped so enforcing 5.x models never reject the request with HTTP 400."""
    home = tmp_path / "contexto-home"
    home.mkdir()
    store = later_session(home, tmp_path)
    gone = store.carried()[0]
    engine = RewriteEngine(store, config=ROOMY)
    upstream_verifier = Upstream()

    # Turn 0 with a signed thinking block from the enforcing fake upstream.
    m0 = [{"role": "user", "content": [{"type": "text", "text": "first prompt"}]}]
    r0 = request(m0)
    up0, _, _ = engine(r0, {}, MESSAGES)
    reply0 = upstream_verifier.respond(up0, None)
    engine.observe_usage(MESSAGES, {"input_tokens": upstream_verifier.tokens(up0), "output_tokens": 50}, r0)

    # Remove the carried record so start_text changes at the next turn start.
    main_close(store, gone.record.id, "done")
    m1 = [*m0, reply0, {"role": "user", "content": [{"type": "text", "text": "second prompt"}]}]
    r1 = request(m1)
    up1, _, ev1 = engine(r1, {}, MESSAGES)

    assert any(e["event"] == "start_text_changed" for e in ev1)
    assert engine.states[engine._main].strip_upto >= len(m1)
    # Must pass strict prefix-binding verification (thinking stripped, no 400!).
    upstream_verifier.verify(up1)
    assert all(b.get("type") not in ("thinking", "redacted_thinking")
               for m in up1["messages"] for b in blocks(m))


def test_carried_reclassified_on_resume_advances_strip_upto():
    """Resuming a v0.2.8 record that reclassifies carried systems must advance strip_upto so no
    surviving thinking block sits after the newly-carried system message."""
    record = json.loads((FIXTURES / "v028_epoch_sonnet5.json").read_text(encoding="utf-8"))
    msgs = session_5x(39, opus=False)
    # Append a turn after record['strip_upto'] carrying a thinking block.
    extra = [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "t", "signature": "sig-old"},
            {"type": "text", "text": "ok"},
        ]},
        *turn_5x(40, opus=False)[:2],
    ]
    final = msgs + extra
    store = EpochStore(record)
    placeholder_pages(store, record["turns_paged"])
    engine = RewriteEngine(store, config=Config(**RESUME_CONFIG))
    up, _, events = engine(request_5x(copy.deepcopy(final)), {}, MESSAGES)

    assert any(e["event"] == "carried_reclassified" for e in events)
    assert engine.states[engine._main].strip_upto >= len(final)
    assert all(b.get("type") not in ("thinking", "redacted_thinking")
               for m in up["messages"] for b in blocks(m))


def test_clipped_fold_note_change_strips_surviving_thinking():
    """When inturn.clipped() cuts or drops a fold (changing messages[start]'s fold note), thinking
    blocks in [start, n) after the fold must be stripped."""
    messages = [{"role": "user", "content": "start"}]
    for r in range(6):
        messages.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"t{r}", "signature": f"sig-{r}"},
            {"type": "tool_use", "id": f"t{r}", "name": "Read", "input": {}},
        ]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{r}", "content": "ok"},
        ]})
    # Fold rounds 0..1 (msgs 1..5), strip only (0, 7), leaving assistant msg 9 with thinking unstripped.
    edits = inturn.InTurn(folds=((0, ((1, 5),), "pg_0123456789abcdef"),), strips=((0, 7),), batches=1)
    # Resync at message 3 cuts the fold back to (1, 3), changing the fold note in messages[0].
    cut = inturn.clipped(edits, 3, messages, ANTHROPIC)
    assert inturn.strips_thinking(cut, 9)
    assert cut.strips == ((0, len(messages)),)


# 3. In-turn PDF & _TURN_NOTES bugs ----------------------------------------------------------------


def test_inturn_document_delegates_to_tokens_pdf_page_count(monkeypatch):
    """_document() must respect PDF_MAX_BASE64 and _PDF_PAGES without calling base64.b64decode or
    count_pages synchronously on huge/cached PDFs."""
    # Oversized payload (> PDF_MAX_BASE64): must return (decoded_size, None) immediately without parsing.
    def boom(*_a, **_kw):
        raise AssertionError("must not parse PDFs over PDF_MAX_BASE64")

    monkeypatch.setattr(tokens, "count_pages", boom)
    huge_b64 = "A" * (PDF_MAX_BASE64 + 4)
    size, pages = inturn._document("application/pdf", huge_b64)
    assert pages is None and size == ((PDF_MAX_BASE64 + 4) // 4) * 3

    # Pre-cached PDF in _PDF_PAGES: must hit _PDF_PAGES without calling count_pages.
    sample = base64.b64encode(b"%PDF-1.7\nsynthetic").decode("ascii")
    tokens._cache_count((len(sample), hash(sample)), tokens.PdfCount(7, True))
    size2, pages2 = inturn._document("application/pdf", sample)
    assert pages2 == 7 and size2 == len(b"%PDF-1.7\nsynthetic")


def test_turn_notes_cache_key_prevents_stale_round_numbers_across_histories():
    """Identical (start, group, listed) over histories with different round counts or tool_use ids
    must not collide in _TURN_NOTES."""
    hooks = inturn.MESSAGES

    def make_history(extra_user_msg: bool, call_prefix: str) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
        if extra_user_msg:
            msgs.append({"role": "system", "content": [{"type": "text", "text": "note"}]})
        for r in range(3):
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": f"{call_prefix}_{r}", "name": "Read", "input": {"file_path": f"f{r}.py"}},
            ]})
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"{call_prefix}_{r}", "content": "ok"},
            ]})
        return msgs

    m_a = make_history(False, "callA")
    # Same group indices ((1, 3),) and ((3, 5),) but in m_b index 1 is a system msg and index 2 is assistant!
    m_b = make_history(True, "callB")
    # m_a: fold1=(1,3) [round 1], fold2=(3,5) [round 2]
    # m_c: same indices as m_a, but with a different call id on round 1
    m_c = make_history(False, "callC")
    group = [(((1, 3),), "pg_1111111111111111"), (((3, 5),), "pg_2222222222222222")]
    note_a = inturn.turn_note(hooks, m_a, m_a, 0, group, (), ANTHROPIC)
    note_c = inturn.turn_note(hooks, m_c, m_c, 0, group, (), ANTHROPIC)
    assert "callA_0" in note_a and "callC_0" in note_c and note_a != note_c

    # Different assistant layout over the same fold span (2, 6):
    # m_b has assistants at 2 and 4 (rounds 1 to 2); m_d has assistant only at 2 (round 1).
    m_d = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "system", "content": [{"type": "text", "text": "note"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "callD_0", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "callD_0", "content": "ok"}]},
        {"role": "system", "content": [{"type": "text", "text": "note2"}]},
        {"role": "user", "content": [{"type": "text", "text": "extra"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "callD_1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "callD_1", "content": "ok"}]},
    ]
    group2 = [(((2, 6),), "pg_3333333333333333"), (((6, 8),), "pg_4444444444444444")]
    note_b = inturn.turn_note(hooks, m_b, m_b, 0, group2, (), ANTHROPIC)
    note_d = inturn.turn_note(hooks, m_d, m_d, 0, group2, (), ANTHROPIC)
    assert "rounds 1 to 2 in pg_3333333333333333" in note_b
    assert "round 1 in pg_3333333333333333" in note_d


def test_massive_unmeasured_uncertain_pdf_is_stubbed_on_arrival():
    """A massive uncounted PDF whose conservative byte floor exceeds config.bound must be guarded
    (stubbed on arrival in Step 0 with documents_over_bound=1) instead of silently blowing past bound."""
    cfg = Config(bound=60_000, headroom_floor=8_000, min_turn_room=4_000)
    # 1.6 MB raw uncounted PDF -> byte floor = 1_600_000 // 20 = 80_000 > bound (60_000).
    massive_pdf = {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf",
        "data": base64.b64encode(b"%PDF-1.7\n" + b"x" * 1_600_000).decode("ascii"),
    }}
    cli = Cli(MemoryStore(key=b"k" * 32), cfg, head_bytes=8_000)
    cli.prompt("Read the huge scan.")
    cli.send([("Read", {"file_path": "huge_scan.pdf"})])
    cli.results([[massive_pdf]])
    sent = cli.send([("Bash", {"command": "echo done"})])

    batch = next(e for e in sent.events if e["event"] == "in_turn_batch")
    assert batch["unseen_results_stubbed"] == 1
    assert batch["documents_over_bound"] == 1
    assert sent.estimate <= cfg.bound
    assert "[Contexto paged out this tool result" in sent.messages[-1]["content"][0]["content"]


# 4. Minor rewrite.py bugs -------------------------------------------------------------------------


def test_minor_rewrite_bug_fixes():
    # (a) strip_carried does not shadow its start parameter across multiple messages.
    msgs = [
        user("p0", context=True),
        assistant("a0"),
        user("p1", context=True),
        assistant("a1"),
    ]
    stripped = strip_carried(msgs, 0, len(msgs))
    assert len(stripped) == 4
    assert all(not any("<system-reminder>" in str(b) for b in m["content"]) for m in stripped if m["role"] == "user")

    # (b) compaction_summary accepts leading whitespace before <system-reminder>.
    compacted_msg = {"role": "user", "content": [
        {"type": "text", "text": "  \n<system-reminder>attribution</system-reminder>"},
        {"type": "text", "text": CLAUDE_CODE_COMPACTION_SUMMARY + "\nSummary body."},
    ]}
    assert compaction_summary([compacted_msg]) is True

    # (c) _anthropic_is_turn_start treats whitespace-only text blocks consistently with whitespace strings.
    assert is_turn_start({"role": "user", "content": "   "}) is False
    assert is_turn_start({"role": "user", "content": [{"type": "text", "text": "   "}]}) is False
    assert is_turn_start({"role": "user", "content": [{"type": "text", "text": ""}]}) is False
    assert is_turn_start({"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>ctx</system-reminder>"},
        {"type": "text", "text": " \t\n "},
    ]}) is False
    assert is_turn_start({"role": "user", "content": [{"type": "text", "text": "hello"}]}) is True


# 5. Active Anthropic cache_control breakpoint management ------------------------------------------


def test_stable_epoch_prefix_breakpoint_and_max_four_limit():
    """When k > 0 and no carried system already carries cache_control, a breakpoint sits on the
    stable epoch prefix (preamble), never exceeding Anthropic's 4-breakpoint limit."""
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    msgs = [user(f"p0 {FILL}", context=True), system("sys", cache=False), assistant(f"a0 {FILL}")]
    for t in range(1, 6):
        msgs += [user(f"p{t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append({"role": "user", "content": [
        {"type": "text", "text": "new prompt", "cache_control": {"type": "ephemeral"}},
    ]})
    req = request(msgs)
    up, st, _ = rewrite(req, None, MemoryStore(key=b"k" * 32), cfg, TokenEstimator())

    assert st.k > 0
    # Preamble block in messages[0] received a cache_control breakpoint.
    assert up["messages"][0]["content"][0].get("cache_control") == {"type": "ephemeral"}
    assert total_breakpoints(up) <= MAX_BREAKPOINTS
    assert _check_breakpoints(up) is None

    # When request ALREADY has 4 breakpoints (2 in head + 2 in retained messages), zero extra are added.
    req_full = {
        **req,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}],
        "system": [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral"}}],
    }
    msgs_full = list(msgs)
    msgs_full[-2] = {"role": "assistant", "content": [
        {"type": "text", "text": f"a5 {FILL}", "cache_control": {"type": "ephemeral"}},
    ]}
    req_full["messages"] = msgs_full
    up_full, st_full, _ = rewrite(req_full, None, MemoryStore(key=b"k" * 32), cfg, TokenEstimator())
    assert st_full.k > 0
    assert total_breakpoints(up_full) <= MAX_BREAKPOINTS
    assert _check_breakpoints(up_full) is None


def test_fold_migrates_cache_control_breakpoint_to_last_surviving_message():
    """Removing a message that holds a cache_control breakpoint during folding migrates that
    breakpoint to the last surviving message before the fold."""
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "turn start prompt"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "res1",
             "cache_control": {"type": "ephemeral"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "b.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "res2"},
        ]},
    ]
    state = EpochState(inturn=inturn.InTurn(folds=((0, ((1, 3),), "pg_0123456789abcdef"),), strips=((0, 5),)))
    viewed = inturn.view(msgs, state, ANTHROPIC)
    assert viewed[1] is inturn.FOLDED and viewed[2] is inturn.FOLDED
    # Breakpoint from folded message 2 migrated to messages[0] (now holding the fold note).
    assert viewed[0]["content"][-1].get("cache_control") == {"type": "ephemeral"}


def test_in_turn_stub_places_breakpoint_at_p_minus_1():
    """When in-turn stubbing runs at index p, a cache_control breakpoint is placed at p - 1 so
    M[k..p) stays a 0.1x cache read, without exceeding 4 breakpoints."""
    cfg = Config(bound=60_000, headroom_floor=8_000, min_turn_room=4_000)
    cli = Cli(MemoryStore(key=b"k" * 32), cfg, head_bytes=8_000)
    cli.prompt("Inspect all modules.")
    cli.loop(12, lambda r: output(f"mod-{r:02d}", 20_480))

    assert cli.state.inturn.stubs and cli.state.inturn.stub_bp > 0
    # Check the last forwarded request that ran with stubs.
    last_req = cli.request()
    up, _, _ = cli.engine(last_req, cli.headers, MESSAGES)
    p = cli.state.inturn.stub_bp
    assert _count_message_bp(up["messages"][p - 1]) == 1
    assert total_breakpoints(up) <= MAX_BREAKPOINTS
    assert _check_breakpoints(up) is None


# 6. Stable prefix ordering across rotations -------------------------------------------------------


def test_stable_session_start_blocks_precede_mutable_preamble_with_breakpoint():
    """Immutable session-start carried blocks (# Environment, CLAUDE.md, skills) sit ahead of the
    mutable rotating preamble with a breakpoint between them, staying byte-identical across rotations."""
    env_block = ctx_kind("# Environment", "OS: macOS, cwd: /repo")
    claude_md = ctx_kind("Codebase and user instructions are shown below", "Always write tests.")
    skills_block = ctx_kind("The following skills are available", "- test-runner")
    volatile_block = ctx_kind("<total_tokens>", "12000")

    msgs = [
        {"role": "user", "content": [env_block, claude_md, skills_block, {"type": "text", "text": f"p0 {FILL}"}]},
        assistant(f"a0 {FILL}"),
        {"role": "user", "content": [volatile_block, {"type": "text", "text": f"p1 {FILL}"}]},
        assistant(f"a1 {FILL}"),
    ]
    for t in range(2, 7):
        msgs += [user(f"p{t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("turn 7 prompt"))

    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    up1, st1, ev1, _ = run(msgs, store=store, config=cfg)
    assert any(e["event"] == "rotation" for e in ev1) and st1.k > 0

    c1 = up1["messages"][0]["content"]
    # Order: [env, claude_md, skills (with cache_control), preamble (with cache_control), volatile, own_prompt]
    assert c1[0] == env_block
    assert c1[1] == claude_md
    assert c1[2]["text"] == skills_block["text"] and c1[2].get("cache_control") == {"type": "ephemeral"}
    assert "Earlier turns" in c1[3]["text"]  # mutable preamble right after immutable prefix (recall salience!)
    assert c1[4] == volatile_block
    assert total_breakpoints(up1) <= MAX_BREAKPOINTS
    assert _check_breakpoints(up1) is None

    # Second rotation: preamble changes (new index_text), yet immutable prefix c[:3] is BYTE-IDENTICAL!
    more = list(msgs) + [assistant(f"a7 {FILL}")]
    for t in range(8, 13):
        more += [user(f"p{t} {FILL}"), assistant(f"a{t} {FILL}")]
    more.append(user("turn 13 prompt"))
    up2, st2, ev2, _ = run(more, state=st1, store=store, config=cfg)
    assert any(e["event"] == "rotation" for e in ev2) and st2.epoch > st1.epoch
    c2 = up2["messages"][0]["content"]
    assert canonical_bytes(c2[:3]) == canonical_bytes(c1[:3])  # 0.1x cache hit across rotations!
    assert c2[3]["text"] != c1[3]["text"]  # preamble rotated

    # Configurable: stable_prefix=False restores preamble-before-all-carried ordering.
    up_off, _, _, _ = run(msgs, store=MemoryStore(key=b"k" * 32), config=replace(cfg, stable_prefix=False))
    assert "Earlier turns" in up_off["messages"][0]["content"][0]["text"]


# 7. Smarter rotation sizing & TTL-aware cold-cache rotation ---------------------------------------


def test_default_window_fraction_and_ttl_aware_cold_cache_rotation():
    """Config().window_fraction defaults to 0.25; a hot cache avoids premature rotation at ~70%
    occupancy, while an expired (>= 300s) Anthropic cache rotates opportunistically (reason='cache_cold')."""
    assert Config().window_fraction == 0.25

    def build_engine(ttl: float = 300.0):
        store = MemoryStore(key=b"k" * 32)
        store.clock = clock = Clock(10_000.0)
        cfg = Config(bound=10_000, headroom_floor=1_000, min_turn_room=1_000, pin_budget=200, index_budget=200,
                     cache_ttl_seconds=ttl, cold_cache_fraction=0.6)
        return RewriteEngine(store, config=cfg), clock

    # Build a 4-turn conversation that lands around ~70% of bound - headroom (9,000), i.e. ~6,500 tokens.
    # Neither size trigger (>9,000) nor room_low (<1,000 -> >8,000) fires at 6,500 tokens!
    msgs = history(3) + [user(f"p3 {FILL}")]

    # (a) Hot cache (elapsed = 60s < 300s): does NOT rotate at ~6,500 tokens.
    eng_hot, clock_hot = build_engine()
    r0 = request(msgs[:-1])
    eng_hot(r0, headers(OLD), MESSAGES)
    eng_hot.observe_usage(MESSAGES, {"input_tokens": 5_800, "output_tokens": 100}, r0, headers=headers(OLD))
    clock_hot.now += 60.0
    _, _, ev_hot = eng_hot(request(msgs), headers(OLD), MESSAGES)
    assert not any(e["event"] == "rotation" for e in ev_hot)

    # (b) Cold cache (elapsed = 301s >= 300s): rotates opportunistically with reason='cache_cold'!
    eng_cold, clock_cold = build_engine()
    eng_cold(r0, headers(OLD), MESSAGES)
    eng_cold.observe_usage(MESSAGES, {"input_tokens": 5_800, "output_tokens": 100}, r0, headers=headers(OLD))
    clock_cold.now += 301.0
    _, _, ev_cold = eng_cold(request(msgs), headers(OLD), MESSAGES)
    rot = next(e for e in ev_cold if e["event"] == "rotation")
    assert rot["reason"] == "cache_cold" and rot["k"] > 0

    # (c) Cold cache at low occupancy (~25% of W): does NOT rotate unnecessarily.
    eng_low, clock_low = build_engine()
    small_msgs = [user("hi"), assistant("hello"), user("how are you?")]
    eng_low(request(small_msgs[:-1]), headers(OLD), MESSAGES)
    eng_low.observe_usage(MESSAGES, {"input_tokens": 1_500, "output_tokens": 50},
                          request(small_msgs[:-1]), headers=headers(OLD))
    clock_low.now += 600.0
    _, _, ev_low = eng_low(request(small_msgs), headers(OLD), MESSAGES)
    assert not any(e["event"] == "rotation" for e in ev_low)

    # (d) 1-hour cache TTL marker (all breakpoints 1h): still hot at 360s, cold at 3,601s.
    #     Mixed TTL (1h head + 5m message breakpoint) treats messages as 5m and rotates at 301s.
    msgs_1h = [user(f"prompt 0 {FILL}", context=True), system(cache=False), assistant(f"answer 0 {FILL}"),
               user(f"prompt 1 {FILL}"), assistant(f"answer 1 {FILL}"),
               user(f"prompt 2 {FILL}"), assistant(f"answer 2 {FILL}"),
               {"role": "user", "content": [{"type": "text", "text": f"p3 {FILL}",
                                             "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}]
    eng_1h, clock_1h = build_engine()
    req_1h_0 = request(msgs_1h[:-1], system=[{"type": "text", "text": "head",
                                              "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    req_1h_1 = request(msgs_1h, system=[{"type": "text", "text": "head",
                                         "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    eng_1h(req_1h_0, headers(OLD), MESSAGES)
    eng_1h.observe_usage(MESSAGES, {"input_tokens": 5_800, "output_tokens": 100}, req_1h_0, headers=headers(OLD))
    clock_1h.now += 360.0
    _, _, ev_6m = eng_1h(req_1h_1, headers(OLD), MESSAGES)
    assert not any(e["event"] == "rotation" for e in ev_6m)

    eng_1h_cold, clock_1h_cold = build_engine()
    eng_1h_cold(req_1h_0, headers(OLD), MESSAGES)
    eng_1h_cold.observe_usage(MESSAGES, {"input_tokens": 5_800, "output_tokens": 100}, req_1h_0, headers=headers(OLD))
    clock_1h_cold.now += 3_601.0
    _, _, ev_61m = eng_1h_cold(req_1h_1, headers(OLD), MESSAGES)
    assert any(e["event"] == "rotation" and e["reason"] == "cache_cold" for e in ev_61m)

    # Mixed TTL: 1h head + 5m messages (msgs[1] has 5m cache_control) rotates at 301s.
    eng_mixed, clock_mixed = build_engine()
    req_m_0 = request(msgs[:-1], system=[{"type": "text", "text": "head",
                                          "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    req_m_1 = request(msgs, system=[{"type": "text", "text": "head",
                                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}])
    eng_mixed(req_m_0, headers(OLD), MESSAGES)
    eng_mixed.observe_usage(MESSAGES, {"input_tokens": 5_800, "output_tokens": 100}, req_m_0, headers=headers(OLD))
    clock_mixed.now += 301.0
    _, _, ev_mixed = eng_mixed(req_m_1, headers(OLD), MESSAGES)
    assert any(e["event"] == "rotation" and e["reason"] == "cache_cold" for e in ev_mixed)

    # (e) Responses dialect (non-Anthropic) does not trigger Anthropic TTL cold-cache rotation.
    store_resp = MemoryStore(key=b"k" * 32)
    store_resp.clock = clock_resp = Clock(10_000.0)
    cfg_resp = Config(bound=10_000, headroom_floor=1_000, min_turn_room=1_000, pin_budget=200, index_budget=200,
                      cache_ttl_seconds=300.0, cold_cache_fraction=0.6)
    eng_resp = RewriteEngine(store_resp, config=cfg_resp)
    resp_items = [{"type": "message", "role": "user", "content": f"p{t} {FILL}"}
                  if t % 2 == 0 else {"type": "message", "role": "assistant", "content": f"a{t} {FILL}"}
                  for t in range(6)]
    req_resp_0 = {"model": "gpt-6-astra", "instructions": "sys", "input": resp_items}
    eng_resp(req_resp_0, {}, "/v1/responses")
    eng_resp.observe_usage("/v1/responses", {"input_tokens": 5_800, "output_tokens": 100}, req_resp_0)
    clock_resp.now += 600.0
    req_resp_1 = {"model": "gpt-6-astra", "instructions": "sys",
                  "input": [*resp_items, {"type": "message", "role": "user", "content": f"p6 {FILL}"}]}
    _, _, ev_resp = eng_resp(req_resp_1, {}, "/v1/responses")
    assert RESPONSES.name != "anthropic"
    assert not any(e["event"] == "rotation" and e.get("reason") == "cache_cold" for e in ev_resp)


def test_mixed_ttl_breakpoints_and_effective_ttl_never_violate_anthropic_ordering():
    """1h head + 5m messages must never stamp ttl='1h' onto transient message breakpoints, must
    count top-level cache_control toward MAX_BREAKPOINTS, and must treat messages' TTL as 5m."""
    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)
    env_block = ctx_kind("# Environment", "OS: macOS")
    msgs = [
        {"role": "user", "content": [env_block, {"type": "text", "text": f"p0 {FILL}"}]},
        assistant(f"a0 {FILL}"),
    ]
    for t in range(1, 6):
        msgs += [user(f"p{t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append({"role": "user", "content": [
        {"type": "text", "text": "tail prompt", "cache_control": {"type": "ephemeral"}},
    ]})

    # Mixed TTL: 1h on system, 5m on tail message.
    req_mixed = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": msgs,
    }
    assert _effective_cache_ttl(req_mixed, cfg) == 300.0
    up, st, _ = rewrite(req_mixed, None, MemoryStore(key=b"k" * 32), cfg, TokenEstimator())
    assert st.k > 0
    assert total_breakpoints(up) <= MAX_BREAKPOINTS
    assert _check_breakpoints(up) is None
    # Every Contexto-added breakpoint on messages[0] must be 5m (no ttl='1h' after head / before 5m tail).
    for blk in up["messages"][0]["content"]:
        if isinstance(blk, dict) and "cache_control" in blk:
            assert blk["cache_control"] == {"type": "ephemeral"}

    # All-1h request (1h on tail message too): earlier Contexto breakpoints inherit 1h to preserve
    # non-increasing TTL ordering (never 5m before 1h!).
    msgs_1h = list(msgs[:-1]) + [{"role": "user", "content": [
        {"type": "text", "text": "tail prompt", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]}]
    req_all_1h = {**req_mixed, "messages": msgs_1h}
    assert _effective_cache_ttl(req_all_1h, cfg) == 3_600.0
    up_1h, _, _ = rewrite(req_all_1h, None, MemoryStore(key=b"k" * 32), cfg, TokenEstimator())
    assert total_breakpoints(up_1h) <= MAX_BREAKPOINTS
    assert _check_breakpoints(up_1h) is None
    for blk in up_1h["messages"][0]["content"]:
        if isinstance(blk, dict) and "cache_control" in blk:
            assert blk["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    # Top-level request.cache_control counts toward MAX_BREAKPOINTS (4).
    req_top = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "cache_control": {"type": "ephemeral"},
        "tools": [{"name": "Read", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}],
        "system": [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral"}}],
        "messages": msgs,
    }
    up_top, _, _ = rewrite(req_top, None, MemoryStore(key=b"k" * 32), cfg, TokenEstimator())
    assert total_breakpoints(up_top) + 1 <= MAX_BREAKPOINTS  # +1 for top-level cache_control


def test_monotonic_clock_on_real_session_store_ignores_wall_clock(tmp_path, monkeypatch):
    """SessionStore sets store.clock = time.time; RewriteEngine._monotonic() must still use
    time.monotonic() so wall-clock jumps never skew TTL or eviction."""
    store = SessionStore(tmp_path, "s_mono")
    engine = RewriteEngine(store)
    monkeypatch.setattr("contexto.rewrite.time.monotonic", lambda: 12_345.5)
    assert engine._monotonic() == 12_345.5


def test_pre_d71_epoch_record_does_not_silently_enable_stable_prefix():
    """A pre-D71 epoch record (no stable_prefix in config) must backfill stable_prefix=False so
    resuming under D71's default stable_prefix=True returns settings_changed instead of silently
    reordering messages[0] under surviving thinking blocks."""
    record = json.loads((FIXTURES / "v028_epoch_sonnet5.json").read_text(encoding="utf-8"))
    assert "stable_prefix" not in record["config"]
    msgs = session_5x(39, opus=False)
    final = msgs + turn_5x(40, opus=False)[:2]

    # Under stable_prefix=True (even with matching window_fraction=0.5): must NOT resume!
    store_on = EpochStore(record)
    placeholder_pages(store_on, record["turns_paged"])
    eng_on = RewriteEngine(store_on, config=Config(**{**RESUME_CONFIG, "stable_prefix": True}))
    _, _, ev_on = eng_on(request_5x(copy.deepcopy(final)), {}, MESSAGES)
    assert any(e["event"] == "epoch_not_resumed" and e["reason"] == "settings_changed" for e in ev_on)


def test_stub_bp_priority_uncacheable_walkback_and_session_store_roundtrip(tmp_path):
    """Uncacheable p - 1 walks back to p - 2; epoch prefix takes priority over stub_bp when only
    one breakpoint slot remains; stub_bp persists cleanly in SessionStore epoch.json."""
    # (a) Uncacheable p - 1 (thinking-only assistant) walks back to p - 2.
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "start prompt"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r1"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": "sig"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "r2"}]},
    ]
    state = EpochState(k=0, inturn=inturn.InTurn(stubs=((4, 0, "pg_0123456789abcdef"),), stub_bp=4, batches=1))
    req = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral"}}],
        "messages": msgs,
    }
    up, _, _ = rewrite(req, state, MemoryStore(key=b"k" * 32), Config(bound=100_000), TokenEstimator())
    assert _count_message_bp(up["messages"][3]) == 0  # thinking-only assistant is uncacheable
    assert _count_message_bp(up["messages"][2]) == 1  # walked back to p - 2!
    assert _check_breakpoints(up) is None

    # (b) Priority: when remaining == 1 with k > 0 and stubs present, preamble_idx wins over stub_bp.
    rot_msgs = [
        user(f"p0 {FILL}"),
        assistant(f"a0 {FILL}"),
        user(f"p1 {FILL}"),
        assistant(f"a1 {FILL}"),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "r3",
                                      "cache_control": {"type": "ephemeral"}}]},
    ]
    st_k = EpochState(epoch=1, k=2, index_text="- pg_1111111111111111: turn 1",
                      hashes=tuple(inturn._rw().message_hash(m) for m in rot_msgs),
                      inturn=inturn.InTurn(stubs=((4, 0, "pg_0123456789abcdef"),), stub_bp=4, batches=1))
    req_tight_bp = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}],
        "system": [{"type": "text", "text": "head", "cache_control": {"type": "ephemeral"}}],
        "messages": rot_msgs,
    }
    # 2 in head + 1 on rot_msgs[4] = 3 already used -> remaining = 1 -> must go to preamble, not p - 1.
    up_prio, _, _ = rewrite(req_tight_bp, st_k, MemoryStore(key=b"k" * 32), Config(bound=100_000), TokenEstimator())
    assert up_prio["messages"][0]["content"][0].get("cache_control") == {"type": "ephemeral"}
    assert _count_message_bp(up_prio["messages"][1]) == 0
    assert total_breakpoints(up_prio) == MAX_BREAKPOINTS

    # (c) SessionStore epoch round-trip with stub_bp > 0.
    store = SessionStore(tmp_path, "s_stub_bp")
    cfg = Config(bound=60_000, headroom_floor=8_000, min_turn_room=4_000)
    cli = Cli(store, cfg, head_bytes=8_000)
    cli.prompt("Inspect all modules.")
    cli.loop(12, lambda r: output(f"mod-{r:02d}", 20_480))
    saved = store.epoch_record()
    assert saved is not None and saved["in_turn"]["stub_bp"] == cli.state.inturn.stub_bp > 0
    restored = inturn.from_record(saved["in_turn"], saved["anchor"]["n"])
    assert restored.stub_bp == cli.state.inturn.stub_bp


def test_stable_prefix_with_preamble_first_false_turn_reminder_count_tokens_and_immutability():
    """Verify stable_prefix=True interacts cleanly with preamble_first=False, turn_reminder,
    count_tokens_view, and never mutates the caller's input request."""
    env_block = ctx_kind("# Environment", "OS: macOS")
    volatile_block = ctx_kind("<total_tokens>", "12000")
    msgs = [
        {"role": "user", "content": [env_block, {"type": "text", "text": f"p0 {FILL}"}]},
        assistant(f"a0 {FILL}"),
        {"role": "user", "content": [volatile_block, {"type": "text", "text": f"p1 {FILL}"}]},
        assistant(f"a1 {FILL}"),
    ]
    for t in range(2, 7):
        msgs += [user(f"p{t} {FILL}"), assistant(f"a{t} {FILL}")]
    msgs.append(user("final turn"))
    req = request(msgs)
    snapshot = copy.deepcopy(req)

    cfg = Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300,
                 stable_prefix=True, preamble_first=False, turn_reminder="Use contexto_recall.")
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    up, _, _ = engine(req, headers(OLD), MESSAGES)
    assert req == snapshot  # input never mutated

    c0 = up["messages"][0]["content"]
    # Order with preamble_first=False: [env_block (stable, bp), volatile_block, preamble (bp), own_prompt, reminder]
    assert c0[0]["text"] == env_block["text"] and c0[0].get("cache_control") == {"type": "ephemeral"}
    assert c0[1] == volatile_block
    assert "Earlier turns" in c0[2]["text"] and c0[2].get("cache_control") == {"type": "ephemeral"}
    assert c0[-1]["text"] == "Use contexto_recall."

    # count_tokens_view via engine(..., COUNT_TOKENS) matches the exact transformed message structure.
    counted, _, _ = engine(req, headers(OLD), COUNT_TOKENS)
    assert canonical_bytes(counted["messages"]) == canonical_bytes(up["messages"])


def test_uncertain_pdf_floor_boundary_stubbed_even_when_plain_savings_below_bound():
    """When an unmeasured uncertain PDF's conservative byte floor barely exceeds config.bound so
    c.plain = u_floor - stub_tokens <= config.bound, Step 0 must still stub it via over_bound_doc."""
    cfg = Config(bound=60_000, headroom_floor=8_000, min_turn_room=4_000)
    # 1,200,400 bytes -> u_floor = 60,020 > bound (60,000), while stub text (~100 tokens) makes c.plain <= 60,000.
    borderline_pdf = {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf",
        "data": base64.b64encode(b"%PDF-1.7\n" + b"x" * 1_200_400).decode("ascii"),
    }}
    cli = Cli(MemoryStore(key=b"k" * 32), cfg, head_bytes=8_000)
    cli.prompt("Read borderline scan.")
    cli.send([("Read", {"file_path": "borderline.pdf"})])
    cli.results([[borderline_pdf]])
    sent = cli.send([("Bash", {"command": "echo done"})])

    batch = next(e for e in sent.events if e["event"] == "in_turn_batch")
    assert batch["unseen_results_stubbed"] == 1
    assert batch["documents_over_bound"] == 1
    assert sent.estimate <= cfg.bound
