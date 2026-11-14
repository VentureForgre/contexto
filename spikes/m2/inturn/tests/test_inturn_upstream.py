"""The fake upstream on its own: each rule it enforces, on a hand-made example, and its tokenizer,
prompt-cache model and rewrite numbers. No engine here: a short conversation goes straight to it."""

from __future__ import annotations

import base64
import copy
import hashlib
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inturn import cli, scripts, tokenizer  # noqa: E402
from inturn.upstream import MESSAGES_KINDS, RESPONSES_KINDS, FakeUpstream  # noqa: E402

from contexto import tokens as engine_tokens  # noqa: E402
from contexto.proxy import serialize_request  # noqa: E402
from contexto.rewrite import dialect_for, usage_tokens  # noqa: E402

ROUNDS = 3
STUB = "[stored as a page]"


def play(dialect: str, *, finish: bool = False, rounds: int = ROUNDS, tokens: int = 60):
    """A prompt and ``rounds`` tool rounds sent straight to a new upstream. Returns the CLI, the
    upstream, the replies, and the turn's last step (the one that ends it)."""
    conversation = scripts.loop120(rounds=rounds, result_tokens=tokens).conversations[0]
    client = cli.CLIS[dialect](conversation)
    upstream = FakeUpstream()
    (turn,) = conversation.turns
    client.prompt(turn)
    replies = []
    for step in turn.steps if finish else turn.steps[:-1]:
        replies.append(upstream.respond("main", client.endpoint, client.request(), step))
        client.take(replies[-1].output, step)
    return client, upstream, replies, turn.steps[-1]


def send(dialect: str, change):
    """The next request of the loop after ``change(request)`` edited a deep copy of it. Returns the
    reply and the upstream."""
    client, upstream, _, last = play(dialect)
    request = copy.deepcopy(client.request())
    change(request)
    return upstream.respond("main", client.endpoint, request, last), upstream


def drive(dialect: str, rounds: int, edit):
    """A whole turn of ``rounds`` rounds where ``edit(i, request)`` edits a deep copy of request ``i``
    before it is sent, as an engine would. Returns the upstream and the replies."""
    conversation = scripts.loop120(rounds=rounds, result_tokens=60).conversations[0]
    client = cli.CLIS[dialect](conversation)
    upstream = FakeUpstream()
    (turn,) = conversation.turns
    client.prompt(turn)
    replies = []
    for i, step in enumerate(turn.steps):
        request = copy.deepcopy(client.request())
        edit(i, request)
        replies.append(upstream.respond("main", client.endpoint, request, step))
        client.take(replies[-1].output, step)
    return upstream, replies


def strip_thinking(message: dict) -> None:
    message["content"] = [b for b in message["content"] if b["type"] != "thinking"]


# The Messages request of play("claude"): 0 prompt, 1 environment (system), then for round r the
# assistant message at 2 + 2r and its tool results at 3 + 2r.
def assistant(r: int) -> int:
    return 2 + 2 * r


def results(r: int) -> int:
    return 3 + 2 * r


# The Responses request of play("codex"): four items before the prompt, the prompt at 4, then for
# round r its reasoning item at 5 + 3r, its call at 6 + 3r and its output at 7 + 3r.
LEAD = 4


def reasoning(r: int) -> int:
    return LEAD + 1 + 3 * r


def call(r: int) -> int:
    return reasoning(r) + 1


def output(r: int) -> int:
    return reasoning(r) + 2


# Messages: pairing and shape ----------------------------------------------------------------------------

def test_a_clean_append_only_conversation_passes_on_both_dialects():
    for dialect in ("claude", "codex"):
        client, upstream, replies, last = play(dialect, finish=True)
        assert [r.violations for r in replies] == [{}] * (ROUNDS + 1)
        assert upstream.violations == {} and upstream.rejected == 0 and upstream.requests == ROUNDS + 1
        assert not any(upstream.info.values())
        assert [r.tool_calls for r in replies] == [True] * ROUNDS + [False]
        assert all(len(r.call_ids) == 1 for r in replies[:-1]) and replies[-1].call_ids == ()
        assert not any(r.rewrite or r.latest_missing for r in replies)


def test_a_tool_use_whose_result_is_not_in_the_next_message():
    reply, upstream = send("claude", lambda r: r["messages"].pop(results(0)))
    assert reply.violations["tool_use_without_result"] == 1
    assert upstream.violations["tool_use_without_result"] == 1 and upstream.rejected == 1


def test_a_message_between_a_tool_use_and_its_result_breaks_both_sides():
    reply, _ = send("claude", lambda r: r["messages"].insert(results(1), {"role": "user", "content": "wait"}))
    assert reply.violations["tool_use_without_result"] == 1
    assert reply.violations["tool_result_without_use"] == 1


def test_a_tool_result_whose_tool_use_is_gone():
    reply, _ = send("claude", lambda r: r["messages"].pop(assistant(1)))
    assert reply.violations["tool_result_without_use"] == 1


def test_a_request_that_ends_on_a_tool_use():
    reply, _ = send("claude", lambda r: r["messages"].pop())
    assert reply.violations == {"tool_use_without_result": 1}


def test_the_same_tool_result_twice_and_the_same_tool_use_twice():
    def result_twice(request):
        content = request["messages"][results(2)]["content"]
        content.append(dict(content[0]))
    assert send("claude", result_twice)[0].violations == {"duplicate_tool_result": 1}

    def use_twice(request):
        content = request["messages"][assistant(2)]["content"]
        content.append(dict(content[-1]))
    assert send("claude", use_twice)[0].violations == {"duplicate_tool_use": 1}


def test_text_before_a_tool_result_is_a_violation_and_text_after_it_is_not():
    """A stub engine's notice ("3 results paged") belongs after the results, where Claude Code puts
    its own reminders."""
    notice = {"type": "text", "text": "<system-reminder>3 results paged</system-reminder>"}
    before, _ = send("claude", lambda r: r["messages"][results(2)]["content"].insert(0, notice))
    assert before.violations == {"text_before_tool_result": 1}
    after, _ = send("claude", lambda r: r["messages"][results(2)]["content"].append(notice))
    assert after.violations == {}


@pytest.mark.parametrize("content", ["", [], [{"type": "text", "text": " \n"}]])
def test_empty_content(content):
    reply, _ = send("claude", lambda r: r["messages"].append({"role": "assistant", "content": content}))
    assert reply.violations == {"empty_content": 1}


def test_an_empty_tool_result_is_allowed():
    def change(request):
        request["messages"][results(2)]["content"][0]["content"] = ""
    assert send("claude", change)[0].violations == {}


def test_a_system_message_before_the_first_user_message():
    reply, _ = send("claude", lambda r: r["messages"].insert(0, {"role": "system", "content": "x"}))
    assert reply.violations["system_before_first_user"] == 1 and "first_message_not_user" not in reply.violations
    assert send("claude", lambda r: r["messages"].append({"role": "system", "content": "x"}))[0].violations == {}


def test_messages_that_start_with_an_assistant_message():
    reply, _ = send("claude", lambda r: r["messages"].insert(0, {"role": "assistant", "content": "Hello."}))
    assert reply.violations["first_message_not_user"] == 1


@pytest.mark.parametrize(("dialect", "field"), [("claude", "messages"), ("codex", "input")])
def test_an_item_that_is_not_an_object(dialect, field):
    reply, upstream = send(dialect, lambda r: r[field].extend(["text", 7]))
    assert reply.violations == {"not_an_object": 2}
    assert upstream.violations == {"not_an_object": 1}


# Messages: preserved thinking ---------------------------------------------------------------------------

def _edit_system(request):
    request["system"][0]["text"] += " Be brief."


def _edit_tools(request):
    request["tools"].pop()


def _edit_first_result(request):
    request["messages"][results(0)]["content"][0]["content"] = "[cleared]"


def _add_text_to_the_prompt(request):
    request["messages"][0]["content"] = [{"type": "text", "text": request["messages"][0]["content"]},
                                         {"type": "text", "text": "One more thing."}]


def _drop_the_oldest_round(request):
    del request["messages"][assistant(0):assistant(1)]


@pytest.mark.parametrize(("change", "bound_blocks"), [
    (_edit_system, 3), (_edit_tools, 3), (_add_text_to_the_prompt, 3),
    (_edit_first_result, 2),  # the first round's thinking came before the result it edits
    (_drop_the_oldest_round, 2),  # client-side trimming: the kept thinking was made with the dropped round
])
def test_a_thinking_block_whose_prefix_changed(change, bound_blocks):
    reply, upstream = send("claude", change)
    assert reply.violations == {"thinking_binding": bound_blocks}
    assert upstream.violations == {"thinking_binding": 1}


def test_a_thinking_block_is_bound_to_what_comes_before_it_in_its_own_message():
    def text_ahead(request):
        request["messages"][assistant(2)]["content"].insert(0, {"type": "text", "text": "Let me check."})
    assert send("claude", text_ahead)[0].violations == {"thinking_binding": 1}

    def moved_after_the_call(request):
        content = request["messages"][assistant(2)]["content"]
        content.append(content.pop(0))
    assert send("claude", moved_after_the_call)[0].violations == {"thinking_binding": 1}

    def text_behind(request):  # after the block: nothing before it changed
        request["messages"][assistant(2)]["content"].insert(1, {"type": "text", "text": "Let me check."})
    assert send("claude", text_behind)[0].violations == {}


def test_moving_the_cache_breakpoint_and_the_prompts_form_change_nothing():
    def change(request):
        for message in request["messages"]:
            if isinstance(message["content"], list):
                for block in message["content"]:
                    block.pop("cache_control", None)
        request["messages"][0]["content"] = [{"type": "text", "text": request["messages"][0]["content"],
                                              "cache_control": {"type": "ephemeral"}}]
    client, _, replies, _ = play("claude")
    reply, _ = send("claude", change)
    assert reply.violations == {} and not reply.rewrite
    assert reply.cache_read == replies[-1].input_tokens  # the whole previous request


def _edit_thinking_text(request):
    request["messages"][assistant(1)]["content"][0]["thinking"] += " And more."


def _edit_signature(request):
    block = request["messages"][assistant(1)]["content"][0]
    block["signature"] = block["signature"][:-1] + ("0" if block["signature"][-1] != "0" else "1")


def _swap_signatures(request):
    first, second = (request["messages"][assistant(r)]["content"][0] for r in (0, 1))
    first["signature"], second["signature"] = second["signature"], first["signature"]


def _unsigned(request):
    del request["messages"][assistant(2)]["content"][0]["signature"]


@pytest.mark.parametrize(("change", "blocks"), [(_edit_thinking_text, 1), (_edit_signature, 1), (_swap_signatures, 2),
                                                (_unsigned, 1)])
def test_a_thinking_block_that_was_altered(change, blocks):
    reply, _ = send("claude", change)
    assert reply.violations == {"thinking_modified": blocks}


# Thinking that goes missing ---------------------------------------------------------------------------
#
# The engine's rule (D63): a batch whose first changed item is at index p strips the thinking of every
# message from p to the end of that request, on that request and on every later one. Thinking before p
# is kept, and thinking the model produces afterwards is kept.

BATCH = 4  # the request that makes the batch: it holds rounds 0 to 3


def batch_messages(i: int, request: dict) -> None:
    """Stub round 1's result and strip the thinking of rounds 2 and 3, from request ``BATCH`` on."""
    if i >= BATCH:
        messages = request["messages"]
        messages[results(1)]["content"][0]["content"] = STUB
        for r in (2, 3):
            strip_thinking(messages[assistant(r)])


def batch_responses(i: int, request: dict) -> None:
    """The same on Codex: stub round 1's output and drop the reasoning items of rounds 2 and 3."""
    if i >= BATCH:
        items = request["input"]
        items[output(1)]["output"] = STUB
        for r in (3, 2):
            del items[reasoning(r)]


def test_thinking_stripped_in_a_rewrites_range_is_accepted_on_that_request_and_later_ones():
    upstream, replies = drive("claude", 6, batch_messages)
    assert [r.violations for r in replies] == [{}] * 7 and upstream.violations == {}
    assert [r.first_changed for r in replies] == [None] * 4 + [results(1), None, None]
    assert [r.latest_missing for r in replies] == [False] * 4 + [True, False, False]
    # Rounds 4 and 5 kept their thinking, bound to the prefix as it was sent to produce them. Requests
    # 5 and 6 therefore hold thinking before and after the stripped range: counted, not rejected.
    assert upstream.info == {"inflight_thinking_missing": 3, "inflight_thinking_missing_latest": 1,
                             "inflight_assistant_absent": 0, "thinking_kept_around_strip": 2}


def test_reasoning_dropped_in_a_rewrites_range_is_accepted_on_that_request_and_later_ones():
    upstream, replies = drive("codex", 6, batch_responses)
    assert [r.violations for r in replies] == [{}] * 7 and upstream.violations == {}
    assert [r.first_changed for r in replies] == [None] * 4 + [output(1), None, None]
    assert [r.latest_missing for r in replies] == [False] * 4 + [True, False, False]
    assert upstream.info == {"inflight_reasoning_dropped": 3, "inflight_reasoning_dropped_latest": 1,
                             "inflight_calls_absent": 0, "reasoning_kept_around_drop": 2}


def test_thinking_missing_before_every_rewrite_or_produced_after_the_latest_is_rejected():
    def before_the_range(i, request):
        batch_messages(i, request)
        if i >= 6:
            strip_thinking(request["messages"][assistant(0)])  # before the batch's first changed item
    _, replies = drive("claude", 6, before_the_range)
    assert [r.violations for r in replies] == [{}] * 6 + [{"thinking_removed_outside_rewrite": 1}]

    def after_the_rewrite(i, request):
        batch_messages(i, request)
        if i >= 6:
            strip_thinking(request["messages"][assistant(4)])  # produced in answer to the batch request
    _, replies = drive("claude", 6, after_the_rewrite)
    assert [r.violations for r in replies] == [{}] * 6 + [{"thinking_removed_outside_rewrite": 1}]

    def before_on_codex(i, request):
        batch_responses(i, request)
        if i >= 6:
            del request["input"][reasoning(0)]
    _, replies = drive("codex", 6, before_on_codex)
    assert [r.violations for r in replies] == [{}] * 6 + [{"reasoning_removed_outside_rewrite": 1}]


def test_the_newest_thinking_stripped_on_a_plain_append_is_rejected():
    reply, upstream = send("claude", lambda r: strip_thinking(r["messages"][assistant(2)]))
    assert reply.violations == {"thinking_removed_outside_rewrite": 1} and reply.latest_missing
    assert not reply.rewrite
    assert upstream.info == {"inflight_thinking_missing": 1, "inflight_thinking_missing_latest": 1,
                             "inflight_assistant_absent": 0, "thinking_kept_around_strip": 0}


def test_stripping_all_thinking_on_every_request_is_rejected_on_every_request():
    def strip_all(i, request):
        for message in request["messages"]:
            if message["role"] == "assistant":
                strip_thinking(message)
    upstream, replies = drive("claude", 6, strip_all)
    assert [r.violations for r in replies] == [{}] + [{"thinking_removed_outside_rewrite": i} for i in range(1, 7)]
    assert sum(r.latest_missing for r in replies) == 6 and not any(r.rewrite for r in replies)

    def drop_all(i, request):
        request["input"] = [item for item in request["input"] if item.get("type") != "reasoning"]
    upstream, replies = drive("codex", 6, drop_all)
    assert [r.violations for r in replies] == [{}] + [{"reasoning_removed_outside_rewrite": i} for i in range(1, 7)]


def test_removing_thinking_alone_breaks_no_api_rule_but_no_rewrite_covers_it():
    """From the start, from the end, or all: the API documents each as valid, so no binding fails. The
    harness still counts it, because nothing else in the request changed."""
    for rounds in ([0], [0, 1], [2], [1, 2], [0, 1, 2]):
        def change(request, rounds=rounds):
            for r in rounds:
                strip_thinking(request["messages"][assistant(r)])
        assert send("claude", change)[0].violations == {"thinking_removed_outside_rewrite": len(rounds)}, rounds


def test_an_inflight_round_that_does_not_arrive_is_counted_apart():
    def change(request):
        del request["messages"][assistant(0):assistant(1)]
        for r in (0, 1):  # what were rounds 1 and 2: stripped, so nothing is bound to the dropped round
            strip_thinking(request["messages"][assistant(r)])
    reply, upstream = send("claude", change)
    assert reply.violations == {} and reply.first_changed == assistant(0)
    assert upstream.info == {"inflight_thinking_missing": 1, "inflight_thinking_missing_latest": 1,
                             "inflight_assistant_absent": 1, "thinking_kept_around_strip": 0}


def test_a_turn_start_rotation_that_strips_thinking_is_covered_and_not_in_flight():
    """What the engine does today at a turn start: the oldest messages go, and the rest lose their thinking."""
    client, upstream, _, _ = play("claude", finish=True)
    client.history.append({"role": "user", "content": "Next task."})
    request = copy.deepcopy(client.request())
    del request["messages"][assistant(0):assistant(1)]
    for message in request["messages"]:
        if message["role"] == "assistant":
            strip_thinking(message)
    assert upstream.respond("main", client.endpoint, request, scripts.Step("thinking")).violations == {}
    assert not any(upstream.info.values())


def test_every_messages_kind_has_an_example_above():
    """The kinds the module lists are the ones these tests raise."""
    def twice(request):
        for index in (results(2), assistant(2)):
            content = request["messages"][index]["content"]
            content.append(dict(content[-1]))
    seen = set()
    for change in (lambda r: r["messages"].pop(results(0)), lambda r: r["messages"].pop(assistant(1)),
                   lambda r: r["messages"].append({"role": "assistant", "content": []}),
                   lambda r: r["messages"].insert(0, {"role": "system", "content": "x"}), _edit_system,
                   _edit_thinking_text, lambda r: r["messages"].append("text"), twice,
                   lambda r: r["messages"].insert(0, {"role": "assistant", "content": "Hello."}),
                   lambda r: r["messages"][results(2)]["content"].insert(0, {"type": "text", "text": "x"}),
                   lambda r: strip_thinking(r["messages"][assistant(2)])):
        seen |= set(send("claude", change)[0].violations)
    assert seen == set(MESSAGES_KINDS)


# Responses ------------------------------------------------------------------------------------------

def test_a_call_without_its_output():
    reply, _ = send("codex", lambda r: r["input"].pop(output(1)))
    assert reply.violations == {"call_without_output": 1}


def test_an_output_without_its_call():
    reply, _ = send("codex", lambda r: r["input"].pop(call(1)))
    assert reply.violations == {"output_without_call": 1, "reasoning_without_following": 1}


def test_an_output_before_its_call_counts_on_both_sides():
    def change(request):
        items = request["input"]
        items[call(1)], items[output(1)] = items[output(1)], items[call(1)]
    assert send("codex", change)[0].violations == {"output_without_call": 1, "call_without_output": 1,
                                                   "reasoning_without_following": 1}


def test_a_call_with_two_outputs():
    reply, _ = send("codex", lambda r: r["input"].append(dict(r["input"][-1])))
    assert reply.violations == {"call_with_several_outputs": 1}


def test_the_same_call_item_twice():
    reply, _ = send("codex", lambda r: r["input"].insert(output(2), dict(r["input"][call(2)])))
    assert reply.violations == {"duplicate_call": 1}


def test_a_function_call_answered_by_a_custom_tool_call_output():
    reply, _ = send("codex", lambda r: r["input"][output(2)].update(type="custom_tool_call_output"))
    assert reply.violations == {"output_kind_mismatch": 1}


def test_other_client_calls_pair_the_same_way_and_hosted_calls_need_no_output():
    def change(request):
        request["input"] += [
            {"type": "custom_tool_call", "call_id": "call_custom", "name": "exec", "input": "ls"},
            {"type": "custom_tool_call_output", "call_id": "call_custom", "output": "ok"},
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
        ]
    assert send("codex", change)[0].violations == {}
    reply, _ = send("codex", lambda r: r["input"].append({"type": "local_shell_call", "call_id": "call_shell"}))
    assert reply.violations == {"call_without_output": 1}


def test_a_reasoning_item_without_the_item_the_model_emitted_after_it():
    reply, _ = send("codex", lambda r: r["input"].append(r["input"][reasoning(0)]))
    assert reply.violations == {"reasoning_without_following": 1}

    def before_a_user_message(request):
        del request["input"][call(2):]
        request["input"].append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]})
    assert send("codex", before_a_user_message)[0].violations == {"reasoning_without_following": 1}


def test_a_reasoning_item_left_behind_when_its_round_was_removed_is_orphaned():
    """Its call and output are gone and the next round's reasoning item follows it: another reasoning
    item is not the item it needs."""
    reply, _ = send("codex", lambda r: r["input"].__delitem__(slice(call(0), reasoning(1))))
    assert reply.violations == {"reasoning_without_following": 1}
    # Removing the whole round, reasoning item included, pairs correctly.
    assert send("codex", lambda r: r["input"].__delitem__(slice(reasoning(0), reasoning(1))))[0].violations == {}


def test_a_reasoning_item_that_was_altered():
    def change(request):
        request["input"][reasoning(1)]["encrypted_content"] += "AAAA"
    assert send("codex", change)[0].violations == {"reasoning_modified": 1}


def test_dropped_inflight_reasoning_and_absent_calls_are_counted():
    def change(request):
        request["input"] = [i for i in request["input"] if i.get("type") != "reasoning"]
    reply, upstream = send("codex", change)
    assert reply.violations == {"reasoning_removed_outside_rewrite": 3}
    assert upstream.info == {"inflight_reasoning_dropped": 1, "inflight_reasoning_dropped_latest": 1,
                             "inflight_calls_absent": 0, "reasoning_kept_around_drop": 0}

    def drop_a_round(request):
        del request["input"][reasoning(0):reasoning(1)]
    reply, upstream = send("codex", drop_a_round)
    assert reply.violations == {}
    assert upstream.info == {"inflight_reasoning_dropped": 0, "inflight_reasoning_dropped_latest": 0,
                             "inflight_calls_absent": 1, "reasoning_kept_around_drop": 0}


def test_every_responses_kind_has_an_example_above():
    seen = set()
    for change in (lambda r: r["input"].pop(output(1)), lambda r: r["input"].pop(call(1)),
                   lambda r: r["input"].append(dict(r["input"][-1])),
                   lambda r: r["input"][reasoning(1)].update(encrypted_content="AAAA"),
                   lambda r: r["input"].append(None),
                   lambda r: r["input"].insert(output(2), dict(r["input"][call(2)])),
                   lambda r: r["input"][output(2)].update(type="custom_tool_call_output"),
                   lambda r: r["input"].pop(reasoning(2))):
        seen |= set(send("codex", change)[0].violations)
    assert seen == set(RESPONSES_KINDS)


# A request is judged as it is sent ------------------------------------------------------------------------

def test_an_object_edited_in_place_after_the_upstream_saw_it_is_judged_as_it_is_now():
    """The same message object, cleared in place between two requests: fresh objects and edited ones
    are judged alike."""
    client, upstream, replies, last = play("claude")
    before = upstream.request_tokens(client.endpoint, client.request())
    client.history[results(0)]["content"][0]["content"] = "[cleared]"
    assert upstream.request_tokens(client.endpoint, client.request()) < before
    reply = upstream.respond("main", client.endpoint, client.request(), last)
    assert reply.violations == {"thinking_binding": 2} and reply.first_changed == results(0)

    client, upstream, _, last = play("claude")
    client.system[0]["text"] += " Be brief."
    client.tools.append({"name": "Extra", "description": "x", "input_schema": {"type": "object"}})
    assert upstream.respond("main", client.endpoint, client.request(), last).violations == {"thinking_binding": 3}


def test_a_pinned_object_is_read_once():
    """Only the harness pins, and only the CLI's own history, which it checks at the end of a run."""
    upstream = FakeUpstream()
    message = {"role": "user", "content": "x" * 310}
    assert upstream.pin("claude", message).tokens == 113  # 4, and its one text block's 335 bytes at 3.1
    message["content"] = "x"
    assert upstream.tokens("claude", message) == 113  # as pinned
    assert upstream.tokens("claude", dict(message)) == 13  # another object: read as it is


# Usage, the cache and the rewrite numbers -----------------------------------------------------------------

def test_usage_has_each_apis_shape_and_the_engine_reads_the_same_total():
    for dialect, endpoint in (("claude", cli.MESSAGES), ("codex", cli.RESPONSES)):
        _, _, replies, _ = play(dialect)
        for reply in replies:
            assert usage_tokens(reply.usage, dialect_for(endpoint)) == reply.input_tokens
            assert reply.usage["output_tokens"] > 0
        last = replies[-1].usage
        if dialect == "claude":
            assert set(last) == {"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                                 "output_tokens"}
            assert last["cache_read_input_tokens"] == replies[-1].cache_read
        else:
            assert last["input_tokens_details"] == {"cached_tokens": replies[-1].cache_read}


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_the_cache_reads_exactly_the_previous_request_on_an_append(dialect):
    _, _, replies, _ = play(dialect, finish=True)
    assert replies[0].cache_read == 0
    for before, after in zip(replies, replies[1:]):
        assert after.cache_read == before.input_tokens
        assert after.input_tokens > before.input_tokens
        assert (after.first_changed, after.invalidated, after.resent) == (None, None, None)


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_an_edit_cuts_the_cache_at_the_first_changed_item(dialect):
    """Invalidated: the previous request from the changed item on. Re-sent: the same items as they are
    now, which the request had to send again; the round added since is growth, not rewrite cost."""
    client, upstream, replies, last = play(dialect)
    field = "messages" if dialect == "claude" else "input"
    request = copy.deepcopy(client.request())
    k = results(1) if dialect == "claude" else output(1)
    new = 2 if dialect == "claude" else 3  # the items of round 2, added since the previous request
    if dialect == "claude":
        request[field][k]["content"][0]["content"] = "[cleared]"
    else:
        request[field][k]["output"] = "[cleared]"
    kept = sum(upstream.tokens(dialect, item) for item in client.items[:k])
    first = 2 if dialect == "claude" else LEAD + 1  # the items of the first request
    head = replies[0].input_tokens - sum(upstream.tokens(dialect, item) for item in client.items[:first])
    reply = upstream.respond("main", client.endpoint, request, last, appended=client.items[-new:])
    assert reply.first_changed == k and reply.cache_read == head + kept
    assert reply.invalidated == replies[-1].input_tokens - (head + kept)
    again = sum(upstream.tokens(dialect, item) for item in request[field][k:-new])
    assert reply.resent == again == reply.input_tokens - reply.cache_read - sum(
        upstream.tokens(dialect, item) for item in client.items[-new:])
    assert reply.resent < reply.invalidated  # the cleared result is smaller than the original


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_a_larger_batch_resends_less_though_it_invalidates_the_same(dialect):
    """Nine rounds of 1,000 tokens, then a request that stubs the first results. Stubbing rounds 0 to 5
    sends rounds 6 to 8 again whole; stubbing rounds 0 to 8 sends only stubs again."""
    field = "messages" if dialect == "claude" else "input"
    index = results if dialect == "claude" else output
    measured = []
    for stubbed in (6, 9):
        client, upstream, replies, last = play(dialect, rounds=9, tokens=1_000)
        request = copy.deepcopy(client.request())
        for r in range(stubbed):
            if dialect == "claude":
                request[field][index(r)]["content"][0]["content"] = STUB
            else:
                request[field][index(r)]["output"] = STUB
        new = client.items[-2:] if dialect == "claude" else client.items[-3:]
        reply = upstream.respond("main", client.endpoint, request, last, appended=new)
        new_tokens = sum(upstream.tokens(dialect, item) for item in new)
        if stubbed == 9:  # the round added since is stubbed too: it counts as it was forwarded
            new_tokens = sum(upstream.tokens(dialect, item) for item in request[field][-len(new):])
        assert reply.resent == reply.input_tokens - reply.cache_read - new_tokens
        measured.append((reply.invalidated, reply.resent, replies[-1].input_tokens))
    (invalidated_6, resent_6, previous), (invalidated_9, resent_9, _) = measured
    assert invalidated_6 == invalidated_9 > 0.3 * previous
    assert resent_9 < 0.5 * resent_6 < 0.25 * invalidated_6


def test_a_head_change_invalidates_the_whole_previous_request():
    for dialect, change in (("claude", _edit_system), ("codex", lambda r: r.update(instructions="other"))):
        client, upstream, replies, last = play(dialect)
        request = copy.deepcopy(client.request())
        change(request)
        new = client.items[-2:] if dialect == "claude" else client.items[-3:]
        reply = upstream.respond("main", client.endpoint, request, last, appended=new)
        assert reply.cache_read == 0 and reply.first_changed == 0
        assert reply.invalidated == replies[-1].input_tokens
        assert reply.resent == reply.input_tokens - sum(upstream.tokens(dialect, item) for item in new)


def test_a_change_to_the_first_message_leaves_the_head_cached():
    client, upstream, replies, last = play("claude")
    request = copy.deepcopy(client.request())
    _add_text_to_the_prompt(request)
    head = upstream.head_tokens(client.endpoint, request)
    reply = upstream.respond("main", client.endpoint, request, last, appended=client.items[-2:])
    assert reply.first_changed == 0 and reply.cache_read == head
    assert reply.invalidated == replies[-1].input_tokens - head


def test_a_request_that_only_drops_the_previous_requests_tail_is_a_rewrite():
    client, upstream, replies, last = play("claude")
    request = copy.deepcopy(client.request())
    del request["messages"][assistant(1):]  # round 1, sent before, and round 2 are gone
    reply = upstream.respond("main", client.endpoint, request, last)
    assert reply.first_changed == assistant(1) and reply.resent == 0
    assert reply.invalidated == replies[-1].input_tokens - reply.input_tokens


def test_wire_bytes_are_the_request_as_the_proxy_writes_it():
    for dialect in ("claude", "codex"):
        client, upstream, _, last = play(dialect)
        request = client.request()
        assert upstream.respond("main", client.endpoint, request, last).wire_bytes == len(serialize_request(request))


# The tokenizer and the images -----------------------------------------------------------------------------

def test_for_text_the_tokenizer_is_not_the_engines_estimator():
    assert (tokenizer.TEXT_BYTES_PER_TOKEN, tokenizer.TOOL_BYTES_PER_TOKEN) == (3.1, 2.2)
    assert tokenizer.TEXT_BYTES_PER_TOKEN != engine_tokens.TEXT_BYTES_PER_TOKEN
    assert tokenizer.TOOL_BYTES_PER_TOKEN != engine_tokens.TOOL_RESULT_BYTES_PER_TOKEN
    text = {"type": "text", "text": "x" * 600}
    result = {"type": "tool_result", "tool_use_id": "t", "content": "x" * 600}
    output_item = {"type": "function_call_output", "call_id": "t", "output": "x" * 600}
    assert tokenizer.block_tokens(text) == -(-len(tokenizer.dumps(text)) * 10 // 31)
    assert tokenizer.block_tokens(result) == -(-len(tokenizer.dumps(result)) * 10 // 22)
    assert tokenizer.item_tokens(output_item) == 4 + -(-len(tokenizer.dumps(output_item)) * 10 // 22)
    assert tokenizer.message_tokens({"role": "user", "content": [text, result]}) == (
        4 + tokenizer.block_tokens(text) + tokenizer.block_tokens(result))
    marked = {**text, "cache_control": {"type": "ephemeral"}}
    assert tokenizer.block_tokens(marked) == tokenizer.block_tokens(text)


@pytest.mark.parametrize(("size", "price"), [((1_280, 800), 1_366), ((1_920, 1_080), 2_765), ((800, 600), 641),
                                             ((2_560, 1_600), 4_787)])
def test_images_cost_the_contracts_rule_3_price_in_both_dialects(size, price):
    """The one part of the tokenizer that is the engine's own rule (``contexto.tokens.image_tokens``)."""
    image = scripts.Image(*size, "seed")
    assert tokenizer.image_cost(cli.claude_image(image)) == price == engine_tokens.image_tokens(*size)
    assert tokenizer.image_cost(cli.codex_image(image)) == price
    text = "x" * 220
    with_image = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": text},
                                                                         cli.claude_image(image)]}
    without = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": text}]}
    assert tokenizer.block_tokens(with_image) == tokenizer.block_tokens(without) + price
    output_item = {"type": "function_call_output", "call_id": "t",
                   "output": [{"type": "input_text", "text": text}, cli.codex_image(image)]}
    assert tokenizer.item_tokens(output_item) == tokenizer.item_tokens({**output_item,
                                                                        "output": output_item["output"][:1]}) + price
    assert engine_tokens.media_cost(engine_tokens.split_media(with_image)[1]).tokens == price


def test_an_image_that_cannot_be_read_costs_the_maximum():
    assert tokenizer.image_cost({"type": "image", "source": {"type": "url", "url": "https://example.test/a.png"}}) == 4_787
    assert tokenizer.image_cost({"type": "input_image", "image_url": "data:image/png;base64,AAAA"}) == 4_787


def test_the_png_is_a_valid_file_with_the_same_bytes_everywhere():
    image = scripts.Image(1_280, 800, "shot-0")
    data = base64.b64decode(scripts.png(image))
    assert engine_tokens.image_size(data[:48]) == (1_280, 800)
    position, chunks = 8, {}
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind, body = data[position + 4:position + 8], data[position + 8:position + 8 + length]
        assert int.from_bytes(data[position + 8 + length:position + 12 + length], "big") == zlib.crc32(kind + body)
        chunks[kind] = body
        position += 12 + length
    assert list(chunks) == [b"IHDR", b"tEXt", b"IDAT", b"IEND"]
    assert zlib.decompress(chunks[b"IDAT"]) == bytes(800 * (1 + 1_280 // 8))  # a filter byte and 160 bytes a row
    assert hashlib.sha256(data).hexdigest()[:16] == PNG_SHA256
    assert scripts.png(scripts.Image(1_280, 800, "shot-1")) != scripts.png(image)


PNG_SHA256 = "715e24dacee0f313"  # of scripts.png(Image(1280, 800, "shot-0")), decoded
