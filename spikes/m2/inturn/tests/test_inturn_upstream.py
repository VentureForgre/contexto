"""The fake upstream on its own: each rule it enforces, on a hand-made example, and its tokenizer,
prompt-cache model and re-sent count. No engine here: a short conversation goes straight to it."""

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


def play(dialect: str, *, finish: bool = False):
    """A prompt and ``ROUNDS`` tool rounds sent straight to a new upstream. Returns the CLI, the
    upstream, the replies, and the turn's last step (the one that ends it)."""
    conversation = scripts.loop120(rounds=ROUNDS, result_tokens=60).conversations[0]
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


def strip_thinking(message: dict) -> None:
    message["content"] = [b for b in message["content"] if b["type"] != "thinking"]


# The Messages request of play("claude"): 0 prompt, 1 environment (system), then for round r the
# assistant message at 2 + 2r and its tool results at 3 + 2r.
def assistant(r: int) -> int:
    return 2 + 2 * r


def results(r: int) -> int:
    return 3 + 2 * r


# Messages -------------------------------------------------------------------------------------------

def test_a_clean_append_only_conversation_passes_on_both_dialects():
    for dialect in ("claude", "codex"):
        client, upstream, replies, last = play(dialect, finish=True)
        assert [r.violations for r in replies] == [{}] * (ROUNDS + 1)
        assert upstream.violations == {} and upstream.rejected == 0 and upstream.requests == ROUNDS + 1
        assert not any(upstream.info.values())
        assert [r.tool_calls for r in replies] == [True] * ROUNDS + [False]
        assert all(len(r.call_ids) == 1 for r in replies[:-1]) and replies[-1].call_ids == ()


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
    assert reply.violations["system_before_first_user"] == 1
    assert send("claude", lambda r: r["messages"].append({"role": "system", "content": "x"}))[0].violations == {}


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


def test_removing_thinking_from_the_start_the_end_or_everywhere_is_valid():
    for rounds in ([0], [0, 1], [2], [1, 2], [0, 1, 2]):
        def change(request, rounds=rounds):
            for r in rounds:
                strip_thinking(request["messages"][assistant(r)])
        assert send("claude", change)[0].violations == {}, rounds


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
    assert reply.violations == {} and reply.resent is None
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


def test_inflight_thinking_that_does_not_come_back_is_counted_not_rejected():
    reply, upstream = send("claude", lambda r: strip_thinking(r["messages"][assistant(2)]))
    assert reply.violations == {}
    assert upstream.info == {"inflight_thinking_missing": 1, "inflight_thinking_missing_latest": 1,
                             "inflight_assistant_absent": 0}
    _, upstream = send("claude", lambda r: strip_thinking(r["messages"][assistant(0)]))
    assert upstream.info == {"inflight_thinking_missing": 1, "inflight_thinking_missing_latest": 0,
                             "inflight_assistant_absent": 0}


def test_an_inflight_round_that_does_not_arrive_is_counted_apart():
    def change(request):
        del request["messages"][assistant(0):assistant(1)]
        for r in (0, 1):  # what were rounds 1 and 2: stripped, so nothing is bound to the dropped round
            strip_thinking(request["messages"][assistant(r)])
    reply, upstream = send("claude", change)
    assert reply.violations == {}
    assert upstream.info == {"inflight_thinking_missing": 1, "inflight_thinking_missing_latest": 1,
                             "inflight_assistant_absent": 1}


def test_thinking_stripped_after_the_turn_ended_is_not_inflight():
    client, upstream, _, _ = play("claude", finish=True)
    client.history.append({"role": "user", "content": "Next task."})
    request = copy.deepcopy(client.request())
    for message in request["messages"]:
        if message["role"] == "assistant":
            strip_thinking(message)
    assert upstream.respond("main", client.endpoint, request, scripts.Step("thinking")).violations == {}
    assert not any(upstream.info.values())


@pytest.mark.parametrize(("dialect", "field"), [("claude", "messages"), ("codex", "input")])
def test_an_item_that_is_not_an_object(dialect, field):
    reply, upstream = send(dialect, lambda r: r[field].extend(["text", 7]))
    assert reply.violations == {"not_an_object": 2}
    assert upstream.violations == {"not_an_object": 1}


def test_every_messages_kind_has_an_example_above():
    """The kinds the module lists are the ones these tests raise."""
    seen = set()
    for change in (lambda r: r["messages"].pop(results(0)), lambda r: r["messages"].pop(assistant(1)),
                   lambda r: r["messages"].append({"role": "assistant", "content": []}),
                   lambda r: r["messages"].insert(0, {"role": "system", "content": "x"}), _edit_system,
                   _edit_thinking_text, lambda r: r["messages"].append("text")):
        seen |= set(send("claude", change)[0].violations)
    assert seen == set(MESSAGES_KINDS)


# Responses ------------------------------------------------------------------------------------------

LEAD = 4  # items before the prompt; then the prompt, then reasoning, call, output per round


def reasoning(r: int) -> int:
    return LEAD + 1 + 3 * r


def test_a_call_without_its_output():
    reply, _ = send("codex", lambda r: r["input"].pop(reasoning(1) + 2))
    assert reply.violations == {"call_without_output": 1}


def test_an_output_without_its_call():
    reply, _ = send("codex", lambda r: r["input"].pop(reasoning(1) + 1))
    assert reply.violations == {"output_without_call": 1, "reasoning_without_following": 1}


def test_an_output_before_its_call_counts_on_both_sides():
    def change(request):
        items = request["input"]
        items[reasoning(1) + 1], items[reasoning(1) + 2] = items[reasoning(1) + 2], items[reasoning(1) + 1]
    assert send("codex", change)[0].violations == {"output_without_call": 1, "call_without_output": 1,
                                                   "reasoning_without_following": 1}


def test_a_call_with_two_outputs():
    reply, _ = send("codex", lambda r: r["input"].append(dict(r["input"][-1])))
    assert reply.violations == {"call_with_several_outputs": 1}


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


def test_a_reasoning_item_without_a_following_model_item():
    reply, _ = send("codex", lambda r: r["input"].append(r["input"][reasoning(0)]))
    assert reply.violations == {"reasoning_without_following": 1}

    def before_a_user_message(request):
        del request["input"][reasoning(2) + 1:]
        request["input"].append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x"}]})
    assert send("codex", before_a_user_message)[0].violations == {"reasoning_without_following": 1}


def test_a_reasoning_item_that_was_altered():
    def change(request):
        request["input"][reasoning(1)]["encrypted_content"] += "AAAA"
    assert send("codex", change)[0].violations == {"reasoning_modified": 1}


def test_dropped_inflight_reasoning_is_counted_not_rejected():
    def change(request):
        request["input"] = [i for i in request["input"] if i.get("type") != "reasoning"]
    reply, upstream = send("codex", change)
    assert reply.violations == {}
    assert upstream.info == {"inflight_reasoning_dropped": 1, "inflight_reasoning_dropped_latest": 1,
                             "inflight_calls_absent": 0}

    def drop_a_round(request):
        del request["input"][reasoning(0):reasoning(1)]
    reply, upstream = send("codex", drop_a_round)
    assert reply.violations == {}
    assert upstream.info == {"inflight_reasoning_dropped": 0, "inflight_reasoning_dropped_latest": 0,
                             "inflight_calls_absent": 1}


def test_every_responses_kind_has_an_example_above():
    seen = set()
    for change in (lambda r: r["input"].pop(reasoning(1) + 2), lambda r: r["input"].pop(reasoning(1) + 1),
                   lambda r: r["input"].append(dict(r["input"][-1])),
                   lambda r: r["input"][reasoning(1)].update(encrypted_content="AAAA"),
                   lambda r: r["input"].append(None)):
        seen |= set(send("codex", change)[0].violations)
    assert seen == set(RESPONSES_KINDS)


# Usage, the cache and the re-sent count ---------------------------------------------------------------

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
        assert after.resent is None


@pytest.mark.parametrize("dialect", ["claude", "codex"])
def test_an_edit_cuts_the_cache_at_the_first_changed_item_and_counts_the_rest_as_resent(dialect):
    client, upstream, replies, last = play(dialect)
    field = "messages" if dialect == "claude" else "input"
    request = copy.deepcopy(client.request())
    k = results(1) if dialect == "claude" else reasoning(1) + 2
    if dialect == "claude":
        request[field][k]["content"][0]["content"] = "[cleared]"
    else:
        request[field][k]["output"] = "[cleared]"
    kept = sum(upstream.tokens(dialect, item) for item in client.items[:k])
    first = 2 if dialect == "claude" else LEAD + 1  # the items of the first request
    head = replies[0].input_tokens - sum(upstream.tokens(dialect, item) for item in client.items[:first])
    reply = upstream.respond("main", client.endpoint, request, last)
    assert reply.cache_read == head + kept
    # The previous request was the one answered by the last round: everything from item k on was sent again.
    assert reply.resent == replies[-1].input_tokens - (head + kept)


def test_a_head_change_resends_the_whole_previous_request():
    for dialect, change in (("claude", _edit_system), ("codex", lambda r: r.update(instructions="other"))):
        client, upstream, replies, last = play(dialect)
        request = copy.deepcopy(client.request())
        change(request)
        reply = upstream.respond("main", client.endpoint, request, last)
        assert reply.cache_read == 0 and reply.resent == replies[-1].input_tokens


def test_wire_bytes_are_the_request_as_the_proxy_writes_it():
    for dialect in ("claude", "codex"):
        client, upstream, _, last = play(dialect)
        request = client.request()
        assert upstream.respond("main", client.endpoint, request, last).wire_bytes == len(serialize_request(request))


# The tokenizer and the images -----------------------------------------------------------------------------

def test_the_tokenizer_is_not_the_engines_estimator():
    assert (tokenizer.TEXT_BYTES_PER_TOKEN, tokenizer.TOOL_BYTES_PER_TOKEN) == (3.1, 2.2)
    assert tokenizer.TEXT_BYTES_PER_TOKEN != engine_tokens.TEXT_BYTES_PER_TOKEN
    assert tokenizer.TOOL_BYTES_PER_TOKEN != engine_tokens.TOOL_RESULT_BYTES_PER_TOKEN
    text = {"type": "text", "text": "x" * 600}
    result = {"type": "tool_result", "tool_use_id": "t", "content": "x" * 600}
    output = {"type": "function_call_output", "call_id": "t", "output": "x" * 600}
    assert tokenizer.block_tokens(text) == -(-len(tokenizer.dumps(text)) * 10 // 31)
    assert tokenizer.block_tokens(result) == -(-len(tokenizer.dumps(result)) * 10 // 22)
    assert tokenizer.item_tokens(output) == 4 + -(-len(tokenizer.dumps(output)) * 10 // 22)
    assert tokenizer.message_tokens({"role": "user", "content": [text, result]}) == (
        4 + tokenizer.block_tokens(text) + tokenizer.block_tokens(result))
    marked = {**text, "cache_control": {"type": "ephemeral"}}
    assert tokenizer.block_tokens(marked) == tokenizer.block_tokens(text)


@pytest.mark.parametrize(("size", "price"), [((1_280, 800), 1_366), ((1_920, 1_080), 2_765), ((800, 600), 641),
                                             ((2_560, 1_600), 4_787)])
def test_images_cost_the_contracts_rule_3_price_in_both_dialects(size, price):
    image = scripts.Image(*size, "seed")
    assert tokenizer.image_cost(cli.claude_image(image)) == price == engine_tokens.image_tokens(*size)
    assert tokenizer.image_cost(cli.codex_image(image)) == price
    text = "x" * 220
    with_image = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": text},
                                                                         cli.claude_image(image)]}
    without = {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": text}]}
    assert tokenizer.block_tokens(with_image) == tokenizer.block_tokens(without) + price
    output = {"type": "function_call_output", "call_id": "t",
              "output": [{"type": "input_text", "text": text}, cli.codex_image(image)]}
    assert tokenizer.item_tokens(output) == tokenizer.item_tokens({**output, "output": output["output"][:1]}) + price
    # The engine prices the same block the same way (D48), whatever its bytes.
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
