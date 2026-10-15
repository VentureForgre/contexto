"""Tests for the M0 fidelity spike: generator, policy, scorer, simulated reader, live runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from fidelity import live, oracle
from fidelity.policy import (
    CONDITIONS,
    RECALL_MAX_CHARS,
    PinnedBudgetError,
    RecallError,
    Settings,
    Store,
    WindowState,
    advance_window,
    append_only,
    build_request,
    check_protocol,
    page_id,
    pinned_claim_errors,
    render_pinned,
    turn_tokens,
)
from fidelity.scoring import normalize, older_values, parse_answers, score
from fidelity.session import Probe, generate
from fidelity.simulate import run_condition

SMALL = 120


@pytest.fixture(scope="module")
def session():
    return generate(turns=SMALL)


# Generator


def test_generator_is_deterministic_per_seed():
    a, b, c = generate(7, SMALL), generate(7, SMALL), generate(8, SMALL)
    assert a.messages == b.messages and a.planted == b.planted and a.probes == b.probes
    assert a.messages != c.messages


def test_generated_messages_follow_protocol(session):
    assert check_protocol(session.messages) == []
    assert session.turns >= SMALL
    for start in session.turn_starts:
        message = session.messages[start]
        assert message["role"] == "user"
        assert all(block["type"] == "text" for block in message["content"])


def test_every_planted_value_is_stated_at_its_recorded_turn(session):
    for planted in session.planted.values():
        for turn, value in planted.history:
            text = json.dumps(session.turn_messages(turn))
            if planted.category == "obligation":
                expected = f"Action item {planted.subject} is done" if value == "closed" else f"Action item {planted.subject}:"
                assert expected in text
            else:
                assert value in text, (planted.key, turn)


def test_planted_mix_covers_every_probe_category(session):
    by_category = {}
    for probe in session.probes:
        by_category.setdefault(probe.category, []).append(probe)
        assert probe.answer == session.planted[probe.key].value
    assert set(by_category) == {"early_fact", "recent_fact", "detail", "current_decision", "obligation_status"}
    assert any(len(p.history) > 1 for p in session.planted.values() if p.category == "decision")
    statuses = {p.value for p in session.planted.values() if p.category == "obligation"}
    assert statuses == {"open", "closed"}
    assert all(p.turn < SMALL // 10 + 2 for p in session.planted.values() if p.key.startswith("fact:") and p.key != "fact:flag")
    ledger_keys = {r["key"] for r in session.ledger}
    assert not any(k.startswith("detail:") for k in ledger_keys)  # details are never pinned


# Scoring


def test_normalize_forgives_case_space_quotes_and_trailing_punctuation():
    assert normalize("  `Amber-1234`. ") == "amber-1234"
    assert normalize('"Open"') == "open"
    assert normalize(None) == ""


def test_parse_answers_takes_last_line_per_id_and_ignores_noise():
    text = "Here you go:\nq_a: one\n- q_b = two\nq_a: three\nnot an answer"
    assert parse_answers(text) == {"q_a": "three", "q_b": "two"}


def test_score_counts_categories_and_flags_stale_values():
    probes = [
        Probe("q_1", "current_decision", "decision:x", "?", "cobalt-2000"),
        Probe("q_2", "early_fact", "fact:y", "?", "delta-3000"),
        Probe("q_3", "early_fact", "fact:z", "?", "ember-4000"),
    ]
    answers = {"q_1": "amber-1000", "q_2": "Delta-3000.", "q_3": "it is ember-4000"}
    result = score(probes, answers, {"decision:x": ["amber-1000"]})
    assert (result.correct, result.total) == (1, 3)
    assert result.by_category == {"current_decision": (0, 1), "early_fact": (1, 2)}
    assert result.stale == ("q_1",)
    assert result.wrong == ("q_1", "q_3")  # a value inside a sentence is not an exact answer


def test_unanswered_probe_is_wrong():
    probe = Probe("q_1", "early_fact", "fact:y", "?", "delta-3000")
    assert score([probe], {}).correct == 0
    assert score([probe], {"q_1": ""}).correct == 0


# Policy


def test_epochs_happen_at_turn_boundaries_with_headroom(session):
    biggest = max(turn_tokens(session, t) for t in range(session.turns))
    settings = Settings(window_high=6_000, window_low=3_000, headroom=biggest)
    state = WindowState()
    previous = 0
    for upto in range(1, session.turns + 1):
        completed = sum(turn_tokens(session, t) for t in range(state.first_turn, upto - 1))
        first = advance_window(session, upto, state, settings)
        assert first >= previous and first <= upto - 1  # the current turn is never paged
        if first != previous:
            assert completed + settings.headroom > settings.window_high
            target = min(settings.window_low, settings.window_high - settings.headroom)
            assert sum(turn_tokens(session, t) for t in range(first, upto - 1)) <= target
        # With headroom covering the largest turn, no request's window goes over the bound.
        assert sum(turn_tokens(session, t) for t in range(first, upto)) <= settings.window_high
        previous = first


def test_a_turn_larger_than_the_headroom_is_reported_over_bound(session):
    settings = Settings(window_high=6_000, window_low=3_000, headroom=0)
    result = run_condition(session, "window", settings)
    assert result.over_bound > 0
    covered = Settings(window_high=6_000, window_low=3_000, headroom=max(turn_tokens(session, t) for t in range(session.turns)))
    assert run_condition(session, "window", covered).over_bound == 0


@pytest.mark.parametrize("condition", CONDITIONS)
def test_every_condition_keeps_protocol_valid_on_every_turn(session, condition):
    settings = Settings(window_high=6_000, window_low=3_000)
    state, store = WindowState(), Store()
    for upto in range(1, session.turns + 1):
        request = build_request(session, condition, upto, settings, state, store)
        assert check_protocol(request.messages) == [], (condition, upto)


def test_check_protocol_detects_a_split_pair(session):
    use_index = next(i for i, m in enumerate(session.messages) if any(b["type"] == "tool_use" for b in m["content"]))
    broken = session.messages[: use_index + 1] + session.messages[use_index + 2 :]
    assert any("not answered" in p for p in check_protocol(broken))
    assert any("no tool_use" in p for p in check_protocol(session.messages[use_index + 1 :]))


def test_large_tool_outputs_are_stubbed_in_place(session):
    settings = Settings(window_high=10**9, window_low=10**9, tool_output_page_over=200)
    store = Store()
    state = WindowState(stub_before=session.turns - 1)  # as if an epoch had just happened
    request = build_request(session, "pinned_recall", session.turns, settings, state, store)
    results = [b for m in request.messages for b in m["content"] if b["type"] == "tool_result"]
    stubbed = [b for b in results if b["content"].startswith("[paged pg_")]
    assert stubbed
    for block in stubbed:
        pid = block["content"].split()[1]
        original = next(
            b["content"] for m in session.messages for b in m["content"]
            if b["type"] == "tool_result" and b["tool_use_id"] == block["tool_use_id"]
        )
        assert store.pages[pid].text == original


def test_page_ids_are_keyed_and_stable():
    payload = {"kind": "turns", "first": 0, "last": 7}
    assert page_id(payload, b"k1") == page_id(payload, b"k1")
    assert page_id(payload, b"k1") != page_id(payload, b"k2")
    assert page_id(payload, b"k1").startswith("pg_") and len(page_id(payload, b"k1")) == 19


def test_turn_page_ids_do_not_change_as_the_session_grows(session):
    settings = Settings(window_high=6_000, window_low=3_000)
    early, late = Store(), Store()
    build_request(session, "pinned_recall", 80, settings, WindowState(), early)
    build_request(session, "pinned_recall", session.turns, settings, WindowState(), late)
    full_groups = {pid: p for pid, p in early.pages.items() if p.kind == "turns" and p.last_turn - p.first_turn == 7}
    assert full_groups
    assert set(full_groups) <= set(late.pages)


def test_recall_round_trips_long_pages_and_reports_errors():
    store = Store()
    from fidelity.policy import Page

    text = "x" * (RECALL_MAX_CHARS + 10)
    store.put(Page("pg_0123456789abcdef", "turns", 0, 7, text, "stub"))
    first = store.recall("pg_0123456789abcdef")
    assert first["next_offset"] == RECALL_MAX_CHARS
    second = store.recall("pg_0123456789abcdef", first["next_offset"])
    assert second["next_offset"] is None and first["text"] + second["text"] == text
    for ref, offset, code in (("pg_ffffffffffffffff", 0, "unknown_id"), ("rec_1", 0, "bad_id"), ("pg_0123456789abcdef", -1, "bad_offset")):
        with pytest.raises(RecallError) as err:
            store.recall(ref, offset)
        assert err.value.code == code


def test_pinned_block_respects_budget_and_never_drops_obligations(session):
    block, omitted = render_pinned(session, session.turns, 2_000)
    assert len(block) <= 2_000 * 4
    open_ids = [p.subject for p in session.planted.values() if p.category == "obligation" and p.value == "open"]
    for oid in open_ids:
        assert f"- {oid}: " in block
    tight, omitted = render_pinned(session, session.turns, 200)
    assert len(tight) <= 200 * 4 and sum(omitted.values()) > 0 and "OMITTED:" in tight
    with pytest.raises(PinnedBudgetError):
        render_pinned(session, session.turns, 20)


# Simulated reader


def test_simulated_conditions_order_as_designed(session):
    settings = Settings(window_high=6_000, window_low=3_000)
    results = {c: run_condition(session, c, settings) for c in CONDITIONS}
    full, window, pinned, recall = (results[c] for c in CONDITIONS)
    assert full.correct == full.total
    assert window.by_category["early_fact"][0] == 0
    assert pinned.by_category["early_fact"] == pinned.by_category["early_fact"][1:] * 2
    assert pinned.by_category["obligation_status"][0] == pinned.by_category["obligation_status"][1]
    assert pinned.by_category["detail"][0] == 0  # details are not pinned
    assert recall.correct == recall.total and recall.recalls > 0
    assert all(r.stale_answers == 0 for r in results.values())
    assert recall.final_tokens < full.final_tokens // 3


def test_reader_does_not_answer_from_text_it_cannot_see(session):
    request = build_request(session, "window", session.turns, Settings(window_high=6_000, window_low=3_000), WindowState())
    early = next(p for p in session.planted.values() if p.key == "fact:staging")
    assert oracle.answer(early, request, Store()).text is None


# Live runner (no model calls)


def test_live_script_is_deterministic_and_dense_adds_unflagged_probes():
    a, b = live.script(3), live.script(3)
    assert a.prompts == b.prompts and a.probes == b.probes
    dense = live.script(3, dense=2)
    unflagged = [p for p in dense.probes if p.category == "detail_unflagged"]
    assert len(unflagged) == 12  # six pasted logs, two lines each
    for probe in dense.probes:
        assert probe.answer == dense.planted[probe.key].value
    prompts = "\n".join(dense.prompts)
    for probe in unflagged:
        assert f"cfg {dense.planted[probe.key].subject}={probe.answer}" in prompts
    assert all(p.id in dense.probe_prompt() for p in dense.probes)


def test_child_env_drops_rig_and_parent_session_variables(monkeypatch):
    monkeypatch.setenv("OPENRIG_SESSION_NAME", "seat")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    env = live.child_env()
    assert "OPENRIG_SESSION_NAME" not in env and "CLAUDE_CODE_SESSION_ID" not in env and "CLAUDECODE" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"


def _fake_result(session_id, usage, model_usage, result="OK"):
    return json.dumps({"session_id": session_id, "result": result, "is_error": False, "usage": usage, "modelUsage": model_usage})


def test_runner_charges_compact_by_model_usage_growth_and_enforces_cap(monkeypatch, tmp_path):
    outputs = iter([
        _fake_result("s1", {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
                     {"m": {"inputTokens": 10, "cacheReadInputTokens": 90, "outputTokens": 5}}),
        _fake_result("s2", {"input_tokens": 0, "output_tokens": 0},
                     {"m": {"inputTokens": 10, "cacheReadInputTokens": 90, "cacheCreationInputTokens": 400, "outputTokens": 55}}),
    ])
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, next(outputs), "")

    monkeypatch.setattr(live.subprocess, "run", fake_run)
    runner = live.Runner(tmp_path, tmp_path / "settings.json", "model-x", cap=100_000)
    runner.call("build_00", "hi", "s1", new=True)
    runner.call("compact", "/compact", "s1", fork=True)
    assert runner.calls[0]["total_tokens"] == 105
    assert runner.calls[1]["total_tokens"] == 450 and runner.calls[1]["usage_source"] == "modelUsage_delta"
    cmd, kwargs = seen[1]
    assert cmd[:3] == ["claude", "-p", "--bare"] and "--fork-session" in cmd and ["--tools", ""] == cmd[cmd.index("--tools"):cmd.index("--tools") + 2]
    assert kwargs["input"] == "/compact" and Path(kwargs["cwd"]) == tmp_path
    tight = live.Runner(tmp_path, tmp_path / "settings.json", "model-x", cap=1_000)
    with pytest.raises(live.CapExceeded):
        tight.call("build_00", "x" * 9_000, "s9", new=True)


# Preserved thinking: every rewrite happens at an epoch; requests in between only append


@pytest.mark.parametrize("condition", ["window", "pinned", "pinned_recall"])
def test_edits_happen_only_at_epochs(session, condition):
    result = run_condition(session, condition, Settings(window_high=6_000, window_low=3_000))
    assert result.window_moves > 0
    assert result.edit_points == result.window_moves
    assert run_condition(session, "full", Settings()).edit_points == 0


def test_current_turn_tool_outputs_are_never_stubbed(session):
    settings = Settings(window_high=6_000, window_low=3_000, tool_output_page_over=200)
    state, store = WindowState(), Store()
    for upto in range(1, session.turns + 1):
        request = build_request(session, "pinned_recall", upto, settings, state, store)
        current = session.turn_messages(upto - 1)
        sent = request.messages[-len(current):]
        for orig, got in zip(current, sent, strict=True):
            for a, b in zip(orig["content"], got["content"][-len(orig["content"]):], strict=True):
                if a["type"] == "tool_result":
                    assert b["content"] == a["content"], upto


def test_append_only_detects_an_edit(session):
    settings = Settings(window_high=10**9, window_low=10**9)
    state = WindowState()
    first = build_request(session, "pinned", 10, settings, state)
    second = build_request(session, "pinned", 11, settings, state)
    assert append_only(first, second)
    edited = build_request(session, "pinned", 11, settings, state)
    edited.messages[0]["content"][0]["text"] += " (edited)"
    assert not append_only(first, edited)


def test_pinned_header_names_its_as_of_turn_and_is_checked(session):
    settings = Settings(window_high=6_000, window_low=3_000)
    state = WindowState()
    for upto in range(1, session.turns + 1):
        request = build_request(session, "pinned", upto, settings, state)
        assert pinned_claim_errors(session, request) == []
    header = next(t for _, t in request.segments if t.startswith("OPEN OBLIGATIONS"))
    assert header.startswith(f"OPEN OBLIGATIONS as of turn {request.window_first_turn - 1} ")
    open_line = next(i for i, (_, t) in enumerate(request.segments) if t.startswith("- ob"))
    request.segments[open_line] = (request.segments[open_line][0], "- ob99: invented [since turn 1]")
    assert pinned_claim_errors(session, request)


def test_base_url_source_reports_without_values(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"apiKeyHelper": "true", "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:2"}}))
    got = live.base_url_source(path)
    assert got == {"source": "settings_env", "equals_inherited": False, "settings_env_keys": ["ANTHROPIC_BASE_URL"]}
    assert "127.0.0.1" not in json.dumps(got)
    path.write_text(json.dumps({"apiKeyHelper": "true"}))
    assert live.base_url_source(path)["source"] == "inherited_env"


def test_cap_is_checked_after_each_call_and_continuations_keep_prior_spend(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        out = _fake_result("s1", {"input_tokens": 5_000, "output_tokens": 0}, {"m": {"inputTokens": 5_000}})
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(live.subprocess, "run", fake_run)
    runner = live.Runner(tmp_path, tmp_path / "settings.json", "model-x", cap=6_000, spent=1_500)
    with pytest.raises(live.CapExceeded):
        runner.call("build_00", "hi", "s1", new=True)  # projected 5,500 passes the guard; recorded 6,500 is over
    calls = [
        {"phase": "build_00", "usage": {"input_tokens": 100}, "model_usage": {"m": {"inputTokens": 100}}},
        {"phase": "compact", "usage": {"input_tokens": 0}, "model_usage": {"m": {"inputTokens": 400, "outputTokens": 50}}},
    ]
    assert live.corrected_spend(calls) == 100 + 350
